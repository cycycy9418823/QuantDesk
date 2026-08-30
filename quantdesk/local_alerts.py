from __future__ import annotations

import json
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Mapping

from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.time_utils import now_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALERTS_PATH = PROJECT_ROOT / "results" / "live_monitor_events.jsonl"
ALERT_DISCLAIMER = "本地提醒只显示规则和风险变化，不接券商，不自动下单。"

ALERT_LABELS = {
    "strategy_triggered": "策略规则触发",
    "risk_line_touched": "风险线触发",
    "data_stale": "数据未更新",
    "market_regime_changed": "市场环境变化",
    "fundamental_red_flag": "财务红线提示",
    "paper_position_review": "虚拟持仓复核",
    "manual_confirmation_pending": "人工确认待处理",
}


_now_text = partial(now_text, sep=" ")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def alert_label(alert_type: str) -> str:
    return ALERT_LABELS.get(alert_type, alert_type or "本地提醒")


def build_local_alert(
    alert_type: str,
    plan: Mapping[str, Any] | Any | None = None,
    extra: Mapping[str, Any] | Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    plan = _mapping(plan)
    extra = _mapping(extra)
    symbol = str(plan.get("symbol") or extra.get("symbol") or "")
    name = str(plan.get("name") or extra.get("name") or "")
    strategy_name = str(plan.get("strategy_name") or extra.get("strategy_name") or "")
    plan_action = str(plan.get("plan_action") or extra.get("plan_action") or "watch")
    plan_action_label = str(plan.get("plan_action_label") or extra.get("plan_action_label") or "继续观察")
    message = str(
        extra.get("message")
        or plan.get("short_explanation")
        or "规则状态发生变化，请进入人工确认清单复核。"
    )
    return {
        "id": f"{alert_type}_{symbol or 'unknown'}_{_now_text(now).replace(' ', '_').replace(':', '')}",
        "created_at": _now_text(now),
        "alert_type": alert_type,
        "alert_label": alert_label(alert_type),
        "symbol": symbol,
        "name": name,
        "strategy_name": strategy_name,
        "plan_action": plan_action,
        "plan_action_label": plan_action_label,
        "confirmation_level": str(plan.get("manual_confirmation_level") or extra.get("confirmation_level") or "normal_review"),
        "data_date_label": str(plan.get("data_date_label") or extra.get("data_date_label") or ""),
        "message": message,
        "risk_notes": list(plan.get("risk_notes") or extra.get("risk_notes") or []),
        "requires_manual_confirmation": True,
        "disclaimer": ALERT_DISCLAIMER,
        **paper_only_boundary(),
    }


def append_local_alert(alert: Mapping[str, Any], path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_ALERTS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(alert)
    payload.setdefault("disclaimer", ALERT_DISCLAIMER)
    payload.update(paper_only_boundary())
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return {"ok": True, "alert": payload, "path": str(target), "disclaimer": ALERT_DISCLAIMER, **paper_only_boundary()}


def load_local_alerts(path: Path | str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    target = Path(path) if path is not None else DEFAULT_ALERTS_PATH
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            item.setdefault("disclaimer", ALERT_DISCLAIMER)
            item.update(paper_only_boundary())
            rows.append(item)
    return rows[-limit:]


def summarize_local_alerts(alerts: list[dict[str, Any]] | None = None) -> dict[str, int]:
    rows = alerts if alerts is not None else load_local_alerts()
    summary: dict[str, int] = {"total": len(rows)}
    for item in rows:
        alert_type = str(item.get("alert_type") or "other")
        summary[alert_type] = summary.get(alert_type, 0) + 1
    return summary


def local_alerts_payload(path: Path | str | None = None, limit: int = 50) -> dict[str, Any]:
    alerts = load_local_alerts(path, limit)
    return {
        "ok": True,
        "alerts": alerts,
        "summary": summarize_local_alerts(alerts),
        "disclaimer": ALERT_DISCLAIMER,
        **paper_only_boundary(),
    }


__all__ = [
    "ALERT_DISCLAIMER",
    "ALERT_LABELS",
    "alert_label",
    "build_local_alert",
    "append_local_alert",
    "load_local_alerts",
    "summarize_local_alerts",
    "local_alerts_payload",
]
