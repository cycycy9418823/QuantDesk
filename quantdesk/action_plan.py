from __future__ import annotations

from datetime import datetime
from functools import partial
from typing import Any, Mapping

from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.time_utils import now_text

ACTION_PLAN_DISCLAIMER = "计划动作仅用于本地模拟验证和人工确认，不接券商，不自动下单，不代表真实收益。"

ACTION_PLAN_LABELS = {
    "none": "无计划动作",
    "watch": "继续观察",
    "virtual_entry_candidate": "虚拟建仓候选",
    "virtual_exit_candidate": "虚拟离场候选",
    "real_world_review_entry": "实战建仓复核",
    "real_world_review_exit": "实战离场复核",
    "risk_review": "风险复核",
}

CONFIRMATION_LEVEL_LABELS = {
    "normal_review": "常规人工确认",
    "strict_review": "严格人工确认",
    "risk_review": "风险复核",
    "blocked": "暂不进入计划动作",
}


_now_text = partial(now_text, sep=" ")


def _m(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _sym(value: Any) -> str:
    text = str(value or "").strip()
    return text.zfill(6) if text.isdigit() else text


def _sf(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if number != number else number


def plan_action_label(plan_action: str) -> str:
    return ACTION_PLAN_LABELS.get(plan_action, ACTION_PLAN_LABELS["watch"])


def confirmation_level_label(level: str) -> str:
    return CONFIRMATION_LEVEL_LABELS.get(level, CONFIRMATION_LEVEL_LABELS["normal_review"])


def _join_notes(*sources: Mapping[str, Any]) -> list[str]:
    notes: list[str] = []
    for source in sources:
        for key in ("risk_notes", "risks", "warnings", "red_flags", "market_notes", "fundamental_notes"):
            for item in _list(source.get(key)):
                text = str(item or "").strip()
                if text and text not in notes:
                    notes.append(text)
    return notes


def _fundamental_status(fundamental_health: Mapping[str, Any]) -> str:
    return str(fundamental_health.get("fundamental_status") or fundamental_health.get("status") or "")


def _paper_summary(paper_status: Mapping[str, Any]) -> Mapping[str, Any]:
    summary = paper_status.get("summary")
    return summary if isinstance(summary, Mapping) else paper_status


def _manual_level(
    plan_action: str,
    trust: Mapping[str, Any],
    market: Mapping[str, Any],
    fundamental: Mapping[str, Any],
    freshness: str,
    blocked: bool,
) -> str:
    if blocked:
        return "blocked"
    if _fundamental_status(fundamental) == "red_flag":
        return "risk_review"
    if freshness in {"source_error", "source_stale", "provider_not_updated_after_close", "mixed_freshness"}:
        return "strict_review"
    if str(market.get("market_regime") or "") == "market_pressure":
        return "strict_review"
    trust_level = str(trust.get("trust_level") or "")
    high_trust = trust_level in {"条件较好", "深度复核", "可观察"}
    if plan_action in {"real_world_review_entry", "real_world_review_exit", "virtual_entry_candidate", "virtual_exit_candidate"}:
        return "normal_review" if high_trust else "strict_review"
    return "risk_review" if plan_action == "risk_review" else "normal_review"


def _review_steps(
    freshness: str,
    market: Mapping[str, Any],
    fundamental: Mapping[str, Any],
    paper_status: Mapping[str, Any],
) -> list[str]:
    steps = [
        "核对数据是否为最新完整日线。",
        "查看市场环境是否承压。",
        "查看财务红线或信息缺失。",
        "查看虚拟组合近期记录。",
        "记录人工复核结论。",
        "如在外部软件处理，需由用户自行确认并备注。",
    ]
    if freshness in {"provider_not_updated_after_close", "source_stale", "source_error"}:
        steps.insert(0, "等待数据源更新后再做正式收盘复盘。")
    if _fundamental_status(fundamental) == "red_flag":
        steps.insert(0, "先复核财务红线，确认是否需要风险复核。")
    if str(market.get("market_regime") or "") == "market_pressure":
        steps.insert(0, "市场环境承压，需提高人工确认等级。")
    if _paper_summary(paper_status).get("position_count"):
        steps.insert(0, "同步查看虚拟持仓状态和风险线。")
    return steps


def _condition_texts(items: Any) -> list[str]:
    texts: list[str] = []
    for item in _list(items):
        if isinstance(item, Mapping):
            text = str(item.get("display_condition") or item.get("explain") or item.get("condition") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            texts.append(text)
    return texts


def _missing_texts(items: Any) -> list[str]:
    texts: list[str] = []
    for item in _list(items):
        if isinstance(item, Mapping):
            text = str(item.get("label") or item.get("field_label") or item.get("field") or item.get("explain") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            texts.append(text)
    return texts


def build_action_plan(
    stock: Mapping[str, Any] | Any,
    strategy_result: Mapping[str, Any] | Any,
    trust_assessment: Mapping[str, Any] | Any | None = None,
    market_regime: Mapping[str, Any] | Any | None = None,
    fundamental_health: Mapping[str, Any] | Any | None = None,
    paper_status: Mapping[str, Any] | Any | None = None,
    data_freshness: Mapping[str, Any] | Any | None = None,
    strategy_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    stock = _m(stock)
    strategy_result = _m(strategy_result)
    trust_assessment = _m(trust_assessment)
    market_regime = _m(market_regime)
    fundamental_health = _m(fundamental_health)
    paper_status = _m(paper_status)
    data_freshness = _m(data_freshness or stock)

    symbol = _sym(stock.get("symbol") or stock.get("code") or strategy_result.get("symbol"))
    name = str(stock.get("name") or strategy_result.get("name") or "")
    strategy_name = str(strategy_name or strategy_result.get("strategy_name") or stock.get("strategy_name") or "本地策略")
    check_type = str(strategy_result.get("check_type") or "entry")
    plan_action = str(strategy_result.get("plan_action") or "watch")

    data_freshness_status = str(
        data_freshness.get("daily_bar_freshness_status")
        or stock.get("feature_freshness")
        or strategy_result.get("feature_freshness")
        or ""
    )
    market_session = str(market_regime.get("market_session") or data_freshness.get("market_session") or "")
    data_reliability = _sf(trust_assessment.get("data_reliability_score"), None)
    blocked_by_trust = bool(strategy_result.get("blocked_by_trust")) or str(trust_assessment.get("action_state") or "") == "暂不参考"
    if data_reliability is not None and data_reliability < 60:
        blocked_by_trust = True
    blocked_by_timing = bool(strategy_result.get("blocked_by_timing"))
    if data_freshness_status in {"source_error", "source_stale"}:
        blocked_by_timing = True
    if market_session in {"non_trading_day", "market_holiday"} and data_freshness_status not in {
        "holiday_using_last_trading_day",
        "current",
        "using_previous_trading_day",
    }:
        blocked_by_timing = True
    if data_freshness_status == "market_not_closed_yet" and plan_action in {"real_world_review_entry", "real_world_review_exit"}:
        blocked_by_timing = True

    if blocked_by_trust or blocked_by_timing:
        plan_action = "risk_review"
    elif plan_action in {"", "none"}:
        plan_action = "watch"

    if plan_action in {"virtual_entry_candidate", "virtual_exit_candidate"}:
        level = _manual_level(plan_action, trust_assessment, market_regime, fundamental_health, data_freshness_status, False)
        if level == "normal_review" and check_type == "entry":
            plan_action = "real_world_review_entry"
        elif level == "normal_review" and check_type == "exit":
            plan_action = "real_world_review_exit"

    manual_confirmation_level = _manual_level(
        plan_action,
        trust_assessment,
        market_regime,
        fundamental_health,
        data_freshness_status,
        blocked_by_trust,
    )
    if plan_action == "risk_review" and manual_confirmation_level == "normal_review":
        manual_confirmation_level = "risk_review"

    triggered_conditions = _condition_texts(strategy_result.get("matched") or strategy_result.get("triggered_conditions"))
    failed_conditions = _condition_texts(strategy_result.get("failed") or strategy_result.get("failed_conditions"))
    missing_fields = _missing_texts(strategy_result.get("missing") or strategy_result.get("missing_fields"))
    risk_notes = _join_notes(strategy_result, trust_assessment, market_regime, fundamental_health, data_freshness)
    if not risk_notes:
        risk_notes = ["请结合数据健康、市场环境和虚拟组合记录复核。"]
    if _fundamental_status(fundamental_health) == "red_flag":
        risk_notes.append("财务红线触发，需要优先风险复核。")
    if str(market_regime.get("market_regime") or "") == "market_pressure":
        risk_notes.append("市场环境承压，计划动作需要更谨慎复核。")
    if data_freshness_status in {"provider_not_updated_after_close", "source_stale", "source_error", "mixed_freshness"}:
        risk_notes.append("当前数据口径不完整，正式复盘需等待完整日线或核对数据源。")

    if blocked_by_trust:
        short_explanation = "数据可信度不足，进入风险复核。"
    elif blocked_by_timing:
        short_explanation = "当前市场时段或数据口径不适合形成计划动作。"
    elif plan_action == "real_world_review_entry":
        short_explanation = "规则条件较完整，可进入人工确认清单。"
    elif plan_action == "real_world_review_exit":
        short_explanation = "观察假设可能失效，可进入人工确认清单。"
    elif plan_action == "virtual_entry_candidate":
        short_explanation = "可先进入虚拟组合验证。"
    elif plan_action == "virtual_exit_candidate":
        short_explanation = "可先在虚拟组合中记录离场复核。"
    elif plan_action == "risk_review":
        short_explanation = "存在阻断或风险项，先做风险复核。"
    else:
        short_explanation = "继续观察，暂不进入计划动作。"

    paper_summary = _paper_summary(paper_status)
    data_date_label = str(
        strategy_result.get("data_date_label")
        or data_freshness.get("daily_bar_freshness_label")
        or stock.get("data_date_label")
        or stock.get("feature_freshness_label")
        or "暂无数据口径"
    )
    return {
        "action_plan_id": f"{symbol or 'unknown'}_{strategy_name}_{_now_text(now).replace(' ', '_').replace(':', '')}",
        "symbol": symbol,
        "name": name,
        "strategy_name": strategy_name,
        "plan_action": plan_action,
        "plan_action_label": plan_action_label(plan_action),
        "manual_confirmation_level": manual_confirmation_level,
        "manual_confirmation_level_label": confirmation_level_label(manual_confirmation_level),
        "triggered_conditions": triggered_conditions,
        "failed_conditions": failed_conditions,
        "missing_fields": missing_fields,
        "risk_notes": list(dict.fromkeys(risk_notes))[:10],
        "data_date_label": data_date_label,
        "suggested_review_steps": _review_steps(data_freshness_status, market_regime, fundamental_health, paper_status),
        "paper_simulation_status": str(
            paper_summary.get("settlement_status")
            or paper_status.get("settlement_status")
            or paper_status.get("status")
            or "暂无虚拟记录"
        ),
        "short_explanation": short_explanation,
        "disclaimer": ACTION_PLAN_DISCLAIMER,
        "blocked_by_trust": blocked_by_trust,
        "blocked_by_timing": blocked_by_timing,
        "data_freshness_status": data_freshness_status or "unknown",
        "market_session": market_session,
        "trust_level": str(trust_assessment.get("trust_level") or ""),
        "trust_score": trust_assessment.get("trust_score"),
        "fundamental_status": _fundamental_status(fundamental_health),
        "market_regime": str(market_regime.get("market_regime") or ""),
        "market_regime_label": str(market_regime.get("market_regime_label") or ""),
        **paper_only_boundary(),
    }


def build_action_plan_overview(
    stocks: list[Mapping[str, Any]] | Any,
    strategy_name: str | None = None,
    trust_map: Mapping[str, Any] | None = None,
    market_regime: Mapping[str, Any] | Any | None = None,
    fundamental_map: Mapping[str, Any] | None = None,
    paper_status_map: Mapping[str, Any] | None = None,
    data_freshness_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows_source = stocks.to_dict("records") if hasattr(stocks, "to_dict") else list(stocks or [])
    trust_map = _m(trust_map)
    fundamental_map = _m(fundamental_map)
    paper_status_map = _m(paper_status_map)
    data_freshness_map = _m(data_freshness_map)
    market_regime = _m(market_regime)
    rows: list[dict[str, Any]] = []
    summary = {key: 0 for key in ACTION_PLAN_LABELS}
    for stock in rows_source:
        if not isinstance(stock, Mapping):
            continue
        symbol = _sym(stock.get("symbol") or stock.get("code"))
        strategy_result = _m(stock.get("detail") or stock.get("strategy_result") or stock)
        trust = _m(trust_map.get(symbol) or stock.get("trust_assessment") or stock.get("trust") or {})
        fundamental = _m(fundamental_map.get(symbol) or stock.get("fundamental_health") or {})
        paper = _m(paper_status_map.get(symbol) or stock.get("paper_status") or {})
        freshness = _m(data_freshness_map.get(symbol) or stock)
        detail = build_action_plan(
            stock,
            strategy_result,
            trust,
            market_regime,
            fundamental,
            paper,
            freshness,
            strategy_name=strategy_name or strategy_result.get("strategy_name") or stock.get("strategy_name"),
        )
        if detail["plan_action"] in summary:
            summary[detail["plan_action"]] += 1
        missing_fields = list(dict.fromkeys(detail.get("missing_fields") or []))
        matched_count = len(detail.get("triggered_conditions") or [])
        failed_count = len(detail.get("failed_conditions") or [])
        missing_count = len(missing_fields)
        if detail.get("blocked_by_trust") or detail.get("blocked_by_timing"):
            status = "blocked"
            status_label = "已阻断"
        elif missing_count:
            status = "missing_required"
            status_label = "缺少关键字段"
        elif failed_count:
            status = "partial"
            status_label = "部分满足"
        else:
            status = "all_matched"
            status_label = "观察条件全部满足"
        rows.append(
            {
                "symbol": detail["symbol"],
                "name": detail["name"],
                "status": status,
                "status_label": status_label,
                "matched_count": matched_count,
                "failed_count": failed_count,
                "missing_count": missing_count,
                "missing_fields": missing_fields,
                "missing_labels": missing_fields,
                "blocked_by_trust": detail.get("blocked_by_trust", False),
                "blocked_by_timing": detail.get("blocked_by_timing", False),
                "short_explanation": detail.get("short_explanation") or "",
                "plan_action": detail["plan_action"],
                "plan_action_label": detail["plan_action_label"],
                "manual_confirmation_level": detail["manual_confirmation_level"],
                "manual_confirmation_level_label": detail["manual_confirmation_level_label"],
                "risk_notes": detail.get("risk_notes") or [],
                "data_date_label": detail.get("data_date_label") or "暂无数据口径",
                "detail": detail,
                "disclaimer": ACTION_PLAN_DISCLAIMER,
                **paper_only_boundary(),
            }
        )
    summary_rows = {
        **summary,
        "all_matched": sum(1 for row in rows if row["status"] == "all_matched"),
        "partial": sum(1 for row in rows if row["status"] == "partial"),
        "blocked": sum(1 for row in rows if row["status"] == "blocked"),
        "missing_required": sum(1 for row in rows if row["status"] == "missing_required"),
    }
    return {
        "strategy_name": strategy_name or "本地策略",
        "total": len(rows),
        "summary": summary_rows,
        "rows": rows,
        "disclaimer": ACTION_PLAN_DISCLAIMER,
        **paper_only_boundary(),
    }


def action_plan_to_dict(plan: Mapping[str, Any] | Any) -> dict[str, Any]:
    return dict(plan or {})


__all__ = [
    "ACTION_PLAN_DISCLAIMER",
    "ACTION_PLAN_LABELS",
    "CONFIRMATION_LEVEL_LABELS",
    "plan_action_label",
    "confirmation_level_label",
    "build_action_plan",
    "build_action_plan_overview",
    "action_plan_to_dict",
]
