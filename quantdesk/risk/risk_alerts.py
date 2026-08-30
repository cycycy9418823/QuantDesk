from __future__ import annotations

import hashlib

from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.platform_common import base_payload, clean_number, finalize_outputs, now_iso, read_json
from quantdesk.risk.position_inputs import load_paper_positions
from quantdesk.risk.smart_stop import build_smart_stop_report

RISK_ALERT_DISCLAIMER = "Research-only risk alerts. No broker. No real orders."


def _alert_id(seed: str) -> str:
    return "risk_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _make_alert(
    *,
    alert_type: str,
    severity: str,
    message: str,
    source: str,
    symbol: str = "",
    name: str = "",
    sector: str = "",
    rationale: list[str] | None = None,
    action: str = "risk_review",
) -> dict:
    dedupe = "|".join([alert_type, severity, symbol, sector, message])
    return {
        "alert_id": _alert_id(dedupe),
        "alert_type": alert_type,
        "severity": severity,
        "symbol": symbol,
        "name": name,
        "sector": sector,
        "message": message,
        "rationale": rationale or [],
        "source": source,
        "triggered_at": now_iso(),
        "dedupe_key": hashlib.sha1(dedupe.encode("utf-8")).hexdigest(),
        **paper_only_boundary(),
        "requires_user_review": True,
        "recommended_action_label": action,
        "disclaimer": "Research-only alert. No real order.",
    }


def evaluate_smart_stop_alerts(smart_stop_payload: dict) -> list[dict]:
    alerts: list[dict] = []
    for item in smart_stop_payload.get("items") or smart_stop_payload.get("alerts") or []:
        if not isinstance(item, dict):
            continue
        status = item.get("trigger_status")
        if status == "triggered":
            alerts.append(
                _make_alert(
                    alert_type="smart_stop",
                    severity="critical",
                    symbol=str(item.get("symbol") or ""),
                    name=str(item.get("name") or ""),
                    sector=str(item.get("sector") or ""),
                    message="Paper stop observation has been triggered",
                    rationale=list(item.get("rationale") or []),
                    source="smart_stop",
                    action="paper_adjustment_review",
                )
            )
        elif status == "watch":
            alerts.append(
                _make_alert(
                    alert_type="smart_stop",
                    severity="warning",
                    symbol=str(item.get("symbol") or ""),
                    name=str(item.get("name") or ""),
                    sector=str(item.get("sector") or ""),
                    message="Paper stop observation is in watch zone",
                    rationale=list(item.get("rationale") or []),
                    source="smart_stop",
                    action="monitor",
                )
            )
    return alerts


def evaluate_position_alerts(positions_payload: dict, config: dict | None = None) -> list[dict]:
    cfg = dict((config or {}).get("risk_alerts", {}).get("concentration") or {})
    symbol_warn = clean_number(cfg.get("single_symbol_weight_warn"), 0.20) or 0.20
    sector_warn = clean_number(cfg.get("sector_weight_warn"), 0.40) or 0.40
    theme_warn = clean_number(cfg.get("theme_weight_warn"), 0.30) or 0.30
    positions = list(positions_payload.get("positions") or [])
    total_value = sum(clean_number(row.get("market_value")) for row in positions)
    sector_weights: dict[str, float] = {}
    theme_weights: dict[str, float] = {}
    alerts: list[dict] = []
    for row in positions:
        weight = clean_number(row.get("weight"))
        if not weight and total_value and row.get("market_value"):
            weight = clean_number(row.get("market_value")) / total_value
        if weight > symbol_warn:
            alerts.append(_make_alert(alert_type="position", severity="warning", symbol=str(row.get("symbol") or ""), name=str(row.get("name") or ""), sector=str(row.get("sector") or ""), message="Single paper position weight exceeds warning threshold", rationale=[f"weight={round(weight, 4)} threshold={symbol_warn}"], source="paper_portfolio", action="risk_review"))
        sector = str(row.get("sector") or "unknown")
        theme = str(row.get("theme") or "")
        sector_weights[sector] = sector_weights.get(sector, 0.0) + weight
        if theme:
            theme_weights[theme] = theme_weights.get(theme, 0.0) + weight
    for sector, weight in sector_weights.items():
        if weight > sector_warn:
            alerts.append(_make_alert(alert_type="position", severity="warning", sector=sector, message="Paper sector exposure exceeds warning threshold", rationale=[f"sector_weight={round(weight, 4)} threshold={sector_warn}"], source="paper_portfolio", action="risk_review"))
    for theme, weight in theme_weights.items():
        if weight > theme_warn:
            alerts.append(_make_alert(alert_type="position", severity="warning", sector=theme, message="Paper theme exposure exceeds warning threshold", rationale=[f"theme_weight={round(weight, 4)} threshold={theme_warn}"], source="paper_portfolio", action="risk_review"))
    return alerts


def evaluate_intraday_alerts(intraday_payload: dict | None, config: dict | None = None) -> list[dict]:
    if not isinstance(intraday_payload, dict):
        return []
    threshold = clean_number(((config or {}).get("risk_alerts", {}).get("intraday") or {}).get("drawdown_from_intraday_high_warn"), 0.03) or 0.03
    alerts: list[dict] = []
    rows = []
    for key in ("quotes", "items", "intraday_scores", "watchlist"):
        value = intraday_payload.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    for row in rows:
        latest = clean_number(row.get("latest_price") or row.get("price") or row.get("close"))
        high = clean_number(row.get("intraday_high") or row.get("high"))
        ref = clean_number(row.get("reference_price") or row.get("prev_close"))
        if high and latest and latest <= high * (1 - threshold):
            alerts.append(_make_alert(alert_type="intraday", severity="warning", symbol=str(row.get("symbol") or ""), name=str(row.get("name") or ""), sector=str(row.get("sector") or ""), message="Intraday drawdown from high exceeds warning threshold", rationale=[f"latest={latest} high={high} threshold={threshold}"], source="intraday_live", action="monitor"))
        if ref and latest and latest <= ref * (1 - threshold):
            alerts.append(_make_alert(alert_type="intraday", severity="warning", symbol=str(row.get("symbol") or ""), name=str(row.get("name") or ""), sector=str(row.get("sector") or ""), message="Intraday price drop from reference exceeds warning threshold", rationale=[f"latest={latest} reference={ref} threshold={threshold}"], source="intraday_live", action="monitor"))
    return alerts


def evaluate_market_alerts(market_payload: dict | None, config: dict | None = None) -> list[dict]:
    if not isinstance(market_payload, dict):
        return []
    threshold = clean_number(((config or {}).get("risk_alerts", {}).get("market") or {}).get("index_drop_3min_warn"), 0.01) or 0.01
    drop = clean_number(market_payload.get("market_index_drop_3min") or market_payload.get("index_drop_3min"))
    if drop > threshold:
        return [_make_alert(alert_type="market", severity="warning", message="Market index drop exceeds local warning threshold", rationale=[f"drop={drop} threshold={threshold}"], source="market_snapshot", action="monitor")]
    return []


def evaluate_sector_alerts(sector_payload: dict | None, positions: list[dict], config: dict | None = None) -> list[dict]:
    if not isinstance(sector_payload, dict):
        return []
    held = {str(row.get("sector") or "") for row in positions if row.get("sector")}
    rows: list[dict] = []
    for key in ("sectors", "sector_rankings", "top_sectors", "items"):
        value = sector_payload.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    alerts = []
    for row in rows:
        sector = str(row.get("sector") or row.get("name") or "")
        if sector not in held:
            continue
        status = str(row.get("sector_status") or row.get("status") or "").lower()
        rank_drop = clean_number(row.get("rank_drop_5d") or -(clean_number(row.get("rank_change_5d"))))
        heat = clean_number(row.get("heat_score"), 100)
        if status == "weakening" or rank_drop >= 5 or heat < 35:
            alerts.append(_make_alert(alert_type="sector", severity="warning", sector=sector, message="Held paper sector is weakening or cooling", rationale=[f"status={status or 'unknown'} rank_drop={rank_drop} heat={heat}"], source="sector_rotation", action="risk_review"))
    return alerts


def evaluate_liquidity_alerts(positions: list[dict], config: dict | None = None) -> list[dict]:
    cfg = dict((config or {}).get("risk_alerts", {}).get("liquidity") or {})
    threshold = clean_number(cfg.get("min_avg_amount_5d"), 50_000_000) or 50_000_000
    alerts = []
    for row in positions:
        amount = clean_number(row.get("avg_amount_5d") or row.get("amount"))
        volume = clean_number(row.get("volume"))
        score = clean_number(row.get("liquidity_score"), 100)
        if amount and amount < threshold:
            alerts.append(_make_alert(alert_type="liquidity", severity="warning", symbol=str(row.get("symbol") or ""), name=str(row.get("name") or ""), sector=str(row.get("sector") or ""), message="Average paper liquidity is below warning threshold", rationale=[f"avg_amount_5d={amount} threshold={threshold}"], source="paper_portfolio", action="data_check"))
        if row.get("latest_price") and volume == 0 and cfg.get("volume_zero_alert", True):
            alerts.append(_make_alert(alert_type="liquidity", severity="critical", symbol=str(row.get("symbol") or ""), name=str(row.get("name") or ""), sector=str(row.get("sector") or ""), message="Latest price exists but volume is zero in local input", rationale=["volume=0"], source="paper_portfolio", action="data_check"))
        if score < 30:
            alerts.append(_make_alert(alert_type="liquidity", severity="warning", symbol=str(row.get("symbol") or ""), name=str(row.get("name") or ""), sector=str(row.get("sector") or ""), message="Liquidity score is low", rationale=[f"liquidity_score={score}"], source="paper_portfolio", action="data_check"))
    return alerts


def build_risk_alerts_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    positions_payload = load_paper_positions(cfg)
    smart_payload = read_json(cfg.get("smart_stop_json") or "results/smart_stop_latest.json", {})
    if cfg.get("synthetic_test_data") or not isinstance(smart_payload, dict) or smart_payload.get("task_type") != "smart_stop":
        smart_payload = build_smart_stop_report(cfg)
    sector_payload = read_json(cfg.get("sector_rotation_json") or "results/sector_rotation_latest.json", {})
    intraday_payload = read_json(cfg.get("intraday_json") or "results/intraday_live_workbench_latest.json", {})
    market_payload = read_json(cfg.get("market_json") or "results/market_snapshot_latest.json", {})
    positions = list(positions_payload.get("positions") or [])
    alerts = []
    alerts.extend(evaluate_smart_stop_alerts(smart_payload if isinstance(smart_payload, dict) else {}))
    alerts.extend(evaluate_position_alerts(positions_payload, cfg))
    alerts.extend(evaluate_liquidity_alerts(positions, cfg))
    alerts.extend(evaluate_intraday_alerts(intraday_payload if isinstance(intraday_payload, dict) else None, cfg))
    alerts.extend(evaluate_sector_alerts(sector_payload if isinstance(sector_payload, dict) else None, positions, cfg))
    alerts.extend(evaluate_market_alerts(market_payload if isinstance(market_payload, dict) else None, cfg))
    warnings = list(positions_payload.get("warnings") or [])
    if not positions:
        warnings.append("risk_alert_inputs_missing")
    critical_count = sum(1 for row in alerts if row.get("severity") == "critical")
    warning_count = sum(1 for row in alerts if row.get("severity") == "warning")
    watch_count = sum(1 for row in alerts if row.get("severity") == "watch")
    status = "warning" if warnings or alerts else "passed"
    payload = base_payload("risk_alerts", status=status, warnings=list(dict.fromkeys(warnings)))
    payload.update(
        {
            **paper_only_boundary(),
            "position_count": len(positions),
            "alert_count": len(alerts),
            "critical_count": critical_count,
            "warning_count_alerts": warning_count,
            "watch_count": watch_count,
            "alerts": alerts,
            "top_alerts": alerts[:5],
            "smart_stop_summary": {
                "status": smart_payload.get("status") if isinstance(smart_payload, dict) else "missing",
                "safe_count": smart_payload.get("safe_count") if isinstance(smart_payload, dict) else 0,
                "watch_count": smart_payload.get("watch_count") if isinstance(smart_payload, dict) else 0,
                "triggered_count": smart_payload.get("triggered_count") if isinstance(smart_payload, dict) else 0,
            },
            "position_input_status": positions_payload.get("input_status", {}),
            "notification_ready": True,
            "warning_count": len(warnings),
            "warnings_top": warnings[:5],
            "disclaimer": RISK_ALERT_DISCLAIMER,
        }
    )
    payload = finalize_outputs(payload, "risk_alerts", render_risk_alerts_markdown(payload))
    if cfg.get("notify"):
        from quantdesk.notifications.notification_center import build_notification_center_report

        notification = build_notification_center_report({**cfg, "from_risk_alerts": payload["output_paths"]["latest_json"]})
        payload["notification_status"] = {
            "status": notification.get("status"),
            "message_count": notification.get("message_count", 0),
            "sent_count": notification.get("sent_count", 0),
            "dry_run": notification.get("dry_run", True),
            "output_paths": notification.get("output_paths", {}),
        }
    return payload


def render_risk_alerts_markdown(payload: dict) -> str:
    lines = [
        "# Risk Alerts",
        "",
        f"- status: `{payload.get('status')}`",
        f"- position_count: `{payload.get('position_count', 0)}`",
        f"- alert_count: `{payload.get('alert_count', 0)}`",
        f"- critical/warning/watch: `{payload.get('critical_count', 0)}` / `{payload.get('warning_count_alerts', 0)}` / `{payload.get('watch_count', 0)}`",
        "",
        "| Severity | Type | Symbol | Sector | Message |",
        "|---|---|---|---|---|",
    ]
    for alert in payload.get("alerts") or []:
        lines.append(f"| {alert.get('severity')} | {alert.get('alert_type')} | {alert.get('symbol') or ''} | {alert.get('sector') or ''} | {alert.get('message') or ''} |")
    if payload.get("warnings"):
        lines += ["", "## Warnings", *[f"- {item}" for item in payload.get("warnings", [])[:10]]]
    lines += ["", RISK_ALERT_DISCLAIMER]
    return "\n".join(lines)
