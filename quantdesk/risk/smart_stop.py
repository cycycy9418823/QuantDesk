from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.platform_common import PROJECT_ROOT, base_payload, clean_number, finalize_outputs, read_json, truthy
from quantdesk.risk.position_inputs import load_paper_positions

SMART_STOP_DISCLAIMER = "Paper-only stop observation. No real order."
PAPER_EXIT_PLAN_DISCLAIMER = (
    "Paper exit review plan only. No broker connection, no real order, and no automatic execution."
)


def _cfg(config: dict | None) -> dict[str, Any]:
    root = dict(config or {})
    smart = dict(root.get("smart_stop") or {})
    smart.setdefault("atr_period", int(root.get("atr_period") or 14))
    smart.setdefault("atr_multiplier", clean_number(root.get("atr_multiplier"), 2.5) or 2.5)
    smart.setdefault("fixed_stop_pct", clean_number(root.get("fixed_stop_pct"), 0.08) or 0.08)
    smart.setdefault("watch_buffer_pct", clean_number(root.get("watch_buffer_pct"), 0.03) or 0.03)
    smart.setdefault("time_stop_enabled", root.get("time_stop_enabled", True))
    smart.setdefault("max_holding_days", int(root.get("max_holding_days") or 20))
    smart.setdefault("min_return_after_holding_days", clean_number(root.get("min_return_after_holding_days"), 0.0))
    smart.setdefault("sector_decay_enabled", root.get("sector_decay_enabled", True))
    smart.setdefault("daily_dir", root.get("daily_dir") or "data/daily/forward_adjusted")
    return smart


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def compute_atr(rows: list[dict], period: int = 14) -> float | None:
    ordered = list(rows or [])
    if len(ordered) < period:
        return None
    sample = ordered[-period:]
    trs: list[float] = []
    prev_close = clean_number(ordered[-period - 1].get("close")) if len(ordered) >= period + 1 else None
    for row in sample:
        high = clean_number(row.get("high"))
        low = clean_number(row.get("low"))
        close = clean_number(row.get("close"))
        if not high or not low:
            continue
        candidates = [high - low]
        if prev_close:
            candidates.extend([abs(high - prev_close), abs(low - prev_close)])
        trs.append(max(candidates))
        prev_close = close or prev_close
    return round(sum(trs) / len(trs), 4) if trs else None


def _read_daily_csv(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(dict(row))
    rows.sort(key=lambda item: str(item.get("date") or item.get("trade_date") or ""))
    return rows


def load_daily_rows_for_position(position: dict, config: dict | None = None) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    symbol = str(position.get("symbol") or "")
    cfg = _cfg(config)
    daily_dir = Path(str((config or {}).get("daily_dir") or cfg.get("daily_dir") or "data/daily/forward_adjusted"))
    if not daily_dir.is_absolute():
        daily_dir = PROJECT_ROOT / daily_dir
    candidates = [daily_dir / f"{symbol}.csv", daily_dir / f"{symbol}.CSV", PROJECT_ROOT / "data" / "daily" / f"{symbol}.csv"]
    for path in candidates:
        rows = _read_daily_csv(path)
        if rows:
            return rows, warnings
    latest = clean_number(position.get("latest_price") or position.get("reference_price"))
    if latest and truthy((config or {}).get("synthetic_test_data")):
        synthetic_rows = []
        for idx in range(20):
            close = round(latest * (0.96 + idx * 0.002), 4)
            synthetic_rows.append({"date": f"2026-05-{idx + 1:02d}", "high": close * 1.02, "low": close * 0.98, "close": close, "volume": 1000000, "amount": close * 1000000})
        warnings.append(f"synthetic_test_data:daily_rows_missing_using_reference:{symbol}")
        return synthetic_rows, warnings
    warnings.append(f"daily_rows_missing:{symbol}")
    return [], warnings


def _sector_for(position: dict, config: dict | None) -> dict | None:
    path = (config or {}).get("sector_rotation_json") or "results/sector_rotation_latest.json"
    payload = read_json(path, {})
    sector = str(position.get("sector") or "")
    rows = []
    if isinstance(payload, dict):
        for key in ("sectors", "sector_rankings", "top_sectors", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
    for row in rows:
        if str(row.get("sector") or row.get("name") or "") == sector:
            return row
    return None


def _holding_days(position: dict, as_of: Any = None) -> int:
    entry = _parse_date(position.get("entry_date"))
    evaluation_day = _parse_date(as_of) if as_of is not None else date.today()
    return (evaluation_day - entry).days if entry and evaluation_day else 0


def build_smart_stop_for_position(
    position: dict,
    daily_rows: list[dict] | None = None,
    sector_status: dict | None = None,
    config: dict | None = None,
    as_of: Any = None,
) -> dict:
    cfg = _cfg(config)
    warnings: list[str] = []
    rows = list(daily_rows or [])
    if not rows:
        rows, warnings = load_daily_rows_for_position(position, config)
    latest_price = clean_number(position.get("latest_price") or (rows[-1].get("close") if rows else 0) or position.get("reference_price"))
    if rows and rows[-1].get("close"):
        latest_price = clean_number(rows[-1].get("close"), latest_price)
    entry_price = clean_number(position.get("entry_price") or position.get("reference_price") or latest_price)
    period = int(cfg.get("atr_period") or 14)
    atr = compute_atr(rows, period=period)
    if atr is None:
        warnings.append("insufficient_daily_rows_for_atr")
    multiplier = clean_number(cfg.get("atr_multiplier"), 2.5) or 2.5
    previous_stop = clean_number(position.get("previous_trailing_stop"))
    atr_stop = round(latest_price - multiplier * atr, 4) if atr is not None and latest_price else None
    if previous_stop and atr_stop:
        atr_stop = max(previous_stop, atr_stop)
    fixed_pct = clean_number(cfg.get("fixed_stop_pct"), 0.08) or 0.08
    fixed_stop = round(entry_price * (1 - fixed_pct), 4) if entry_price else None
    candidates = [value for value in (atr_stop, fixed_stop) if value is not None]
    composite_stop = max(candidates) if candidates else None
    distance = round((latest_price - composite_stop) / latest_price, 6) if latest_price and composite_stop else None
    status = "unknown"
    severity = "info"
    rationale = []
    if latest_price and composite_stop:
        if latest_price <= composite_stop:
            status = "triggered"
            severity = "critical"
            rationale.append("latest price is at or below the paper stop observation line")
        elif latest_price <= composite_stop * (1 + clean_number(cfg.get("watch_buffer_pct"), 0.03)):
            status = "watch"
            severity = "warning"
            rationale.append("latest price is within the configured watch buffer")
        else:
            status = "safe"
            severity = "info"
            rationale.append("latest price is above the paper stop observation line")
    holding_days = _holding_days(position, as_of=as_of)
    if cfg.get("time_stop_enabled") and holding_days > int(cfg.get("max_holding_days") or 20):
        ret = round((latest_price - entry_price) / entry_price, 6) if latest_price and entry_price else 0
        if ret <= clean_number(cfg.get("min_return_after_holding_days"), 0.0) and status != "triggered":
            status = "watch"
            severity = "warning"
            rationale.append("holding time exceeded configured review horizon")
    sector = sector_status or _sector_for(position, config)
    sector_label = str((sector or {}).get("sector_status") or (sector or {}).get("status") or "").lower()
    rank_drop = clean_number((sector or {}).get("rank_drop_5d") or -(clean_number((sector or {}).get("rank_change_5d"))))
    heat = clean_number((sector or {}).get("heat_score"), 100)
    if cfg.get("sector_decay_enabled") and sector:
        if sector_label == "weakening" or rank_drop >= 5 or heat < 35:
            if status != "triggered":
                status = "watch"
                severity = "warning"
            rationale.append("sector rotation context is weakening or cooling")
    drawdown = round((latest_price - entry_price) / entry_price, 6) if latest_price and entry_price else None
    item = {
        "symbol": position.get("symbol"),
        "name": position.get("name") or "",
        "sector": position.get("sector") or "",
        "strategy": position.get("strategy") or "",
        "source": position.get("source") or "",
        "latest_price": latest_price or None,
        "entry_price": entry_price or None,
        "entry_date": position.get("entry_date"),
        "holding_days": holding_days,
        "atr": atr,
        "atr_period": period,
        "atr_multiplier": multiplier,
        "atr_stop_price": atr_stop,
        "fixed_stop_price": fixed_stop,
        "composite_stop_price": composite_stop,
        "distance_to_stop_pct": distance,
        "drawdown_from_entry": drawdown,
        "trigger_status": status,
        "severity": severity,
        "trigger_reason": "; ".join(rationale[:3]) if rationale else "insufficient data for stop status",
        "rationale": rationale or ["insufficient data for stop status"],
        "warnings": warnings,
        **paper_only_boundary(),
        "disclaimer": SMART_STOP_DISCLAIMER,
    }
    item["paper_exit_plan"] = build_paper_exit_plan_from_smart_stop(item)
    return item


def build_paper_exit_plan_from_smart_stop(item: dict) -> dict:
    """Convert a smart-stop observation into a reusable paper-exit review plan.

    The plan is intentionally not an order.  It gives the paper account, UI, and
    agent bridge one shared contract for "this holding needs exit review" when a
    stop observation is triggered, without connecting a broker or auto-selling.
    """

    symbol = str(item.get("symbol") or "")
    latest_price = clean_number(item.get("latest_price"))
    risk_line = clean_number(item.get("composite_stop_price"))
    trigger_status = str(item.get("trigger_status") or "unknown")
    available = bool(symbol and latest_price and risk_line)
    triggered = trigger_status == "triggered"
    watch = trigger_status == "watch"
    plan_status = "triggered" if triggered else "watch" if watch else "inactive" if available else "unavailable"
    condition = {
        "rule_id": "smart_stop_triggered_exit_review",
        "field": "close",
        "op": "<=",
        "value": risk_line,
        "source": "smart_stop",
        "condition_type": "forced_exit_review",
        "rule_action": "paper_exit_review",
        "force_exit_rule": bool(triggered),
        **paper_only_boundary(),
        "explain": "close is at or below the paper risk observation line",
    } if risk_line else {}
    latest_daily = {
        "close": latest_price,
        "risk_observation_line": risk_line,
        "smart_stop_status": trigger_status,
    }
    return {
        "available": available,
        "plan_type": "paper_exit_review",
        "plan_status": plan_status,
        "symbol": symbol,
        "name": item.get("name") or "",
        "strategy": item.get("strategy") or "",
        "entry_date": item.get("entry_date") or "",
        "latest_price": latest_price or None,
        "risk_observation_line": risk_line or None,
        "trigger_reason": item.get("trigger_reason") or "",
        "rationale": list(item.get("rationale") or []),
        "paper_exit_candidate": bool(triggered),
        "forced_exit_review": bool(triggered),
        "requires_manual_review": bool(triggered or watch),
        "auto_execute": False,
        "execution_allowed": False,
        **paper_only_boundary(),
        "condition_plan": {
            "symbol": symbol,
            "name": item.get("name") or "",
            "strategy": item.get("strategy") or "",
            "latest_daily": latest_daily,
            "exit_conditions": [dict(condition)] if condition else [],
            "forced_exit_rules": [dict(condition)] if triggered and condition else [],
            "entry_conditions": [],
            "execution_allowed": False,
            "fill_model": "none_paper_review_only",
            "paper_exit_candidate": bool(triggered),
            "forced_exit_review": bool(triggered),
            **paper_only_boundary(),
        },
        "disclaimer": PAPER_EXIT_PLAN_DISCLAIMER,
    }


def compute_trailing_stop(position: dict, daily_rows: list[dict], config: dict | None = None) -> dict:
    item = build_smart_stop_for_position(position, daily_rows=daily_rows, config=config)
    return {
        "symbol": item.get("symbol"),
        "current_price": item.get("latest_price"),
        "atr": item.get("atr"),
        "stop_price": item.get("atr_stop_price"),
        "stop_type": "atr_trailing_stop",
        "stop_distance_pct": item.get("distance_to_stop_pct"),
        "trigger_status": item.get("trigger_status"),
        **paper_only_boundary(),
    }


def compute_time_stop(position: dict, as_of_date: str, config: dict | None = None) -> dict:
    item = build_smart_stop_for_position(
        {**position, "entry_date": position.get("entry_date") or as_of_date},
        config=config,
        as_of=as_of_date,
    )
    return {"symbol": item.get("symbol"), "as_of_date": as_of_date, "stop_type": "time_stop", "trigger_status": item.get("trigger_status"), "rationale": item.get("rationale"), **paper_only_boundary()}


def compute_sector_decay_stop(position: dict, sector_status: dict | None, config: dict | None = None) -> dict:
    item = build_smart_stop_for_position(position, sector_status=sector_status, config=config)
    return {"symbol": item.get("symbol"), "sector_status": (sector_status or {}).get("sector_status") or "unknown", "stop_type": "sector_decay_stop", "trigger_status": item.get("trigger_status"), **paper_only_boundary()}


def build_smart_stop_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    positions_payload = load_paper_positions(cfg)
    items = []
    warnings = list(positions_payload.get("warnings") or [])
    for position in positions_payload.get("positions") or []:
        item = build_smart_stop_for_position(position, config=cfg)
        warnings.extend(str(w) for w in item.get("warnings") or [])
        items.append(item)
    safe_count = sum(1 for item in items if item.get("trigger_status") == "safe")
    watch_count = sum(1 for item in items if item.get("trigger_status") == "watch")
    triggered_count = sum(1 for item in items if item.get("trigger_status") == "triggered")
    status = "warning" if warnings or watch_count or triggered_count else "passed"
    payload = base_payload("smart_stop", status=status, warnings=list(dict.fromkeys(warnings)))
    payload.update(
        {
            **paper_only_boundary(),
            "items": items,
            "alerts": items,
            "position_count": len(items),
            "safe_count": safe_count,
            "watch_count": watch_count,
            "triggered_count": triggered_count,
            "warning_count": len(payload["warnings"]),
            "warnings_top": payload["warnings"][:5],
            "position_input_status": positions_payload.get("input_status", {}),
            "disclaimer": SMART_STOP_DISCLAIMER,
        }
    )
    return finalize_outputs(payload, "smart_stop", render_smart_stop_markdown(payload))


def render_smart_stop_markdown(payload: dict) -> str:
    lines = [
        "# Smart Stop",
        "",
        f"- status: `{payload.get('status')}`",
        f"- position_count: `{payload.get('position_count', 0)}`",
        f"- safe/watch/triggered: `{payload.get('safe_count', 0)}` / `{payload.get('watch_count', 0)}` / `{payload.get('triggered_count', 0)}`",
        "",
        "| Symbol | Status | Severity | Stop | Latest | Rationale |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in payload.get("items") or []:
        lines.append(
            f"| {item.get('symbol') or ''} | {item.get('trigger_status') or ''} | {item.get('severity') or ''} | "
            f"{item.get('composite_stop_price') or ''} | {item.get('latest_price') or ''} | {'; '.join(item.get('rationale') or [])} |"
        )
    if payload.get("warnings"):
        lines += ["", "## Warnings", *[f"- {item}" for item in payload.get("warnings", [])[:10]]]
    lines += ["", SMART_STOP_DISCLAIMER]
    return "\n".join(lines)
