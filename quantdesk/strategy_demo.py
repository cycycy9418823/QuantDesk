from __future__ import annotations

from typing import Any, Mapping

# evaluate_condition 由本模块底部的兼容包装函数提供（Round 21 F811 清理：
# 不再导入同名符号，避免遮蔽歧义）。
from quantdesk.strategy_conditions import evaluate_conditions
from quantdesk.strategy_features import build_strategy_feature_row, safe_float
from quantdesk.strategy_registry import (
    STRATEGY_DEMO_DISCLAIMER as REGISTRY_STRATEGY_DEMO_DISCLAIMER,
)
from quantdesk.strategy_registry import (
    load_strategy_registry,
    resolve_strategy,
)

STRATEGY_DEMO_VERSION = "0.3.0"
STRATEGY_DEMO_DISCLAIMER = REGISTRY_STRATEGY_DEMO_DISCLAIMER
STRATEGY_CONFIG_PATH = None
STRATEGIES: dict[str, dict[str, Any]] = {}
STRATEGY_CONFIG_WARNING = ""

STATUS_LABELS = {
    "all_matched": "观察条件全部满足",
    "partial": "部分满足",
    "blocked": "已阻断",
    "missing_required": "缺少关键字段",
}

FIELD_LABELS = {
    "close": "收盘价",
    "ma20": "20日均线",
    "ma60": "60日均线",
    "ma5": "5日均线",
    "ma10": "10日均线",
    "volume": "成交量",
    "avg_volume_20": "20日均量",
    "activity_value": "成交活跃度",
    "activity_avg_20": "20日均成交活跃度",
    "activity_ratio_20": "成交活跃度比例",
    "data_reliability_score": "数据可信度",
    "high_20d": "20日新高",
    "macd_dif": "MACD DIF",
    "macd_dea": "MACD DEA",
    "macd_histogram": "MACD 柱体",
    "previous_macd_histogram": "上一根 MACD 柱体",
    "rsi14": "RSI14",
    "boll_lower": "BOLL 下轨",
    "previous_close": "前收盘",
    "vol20": "20日波动率",
}

PLAN_ACTION_LABELS = {
    "none": "无计划动作",
    "watch": "继续观察",
    "virtual_entry_candidate": "进入虚拟建仓确认",
    "virtual_exit_candidate": "进入虚拟离场确认",
    "risk_review": "进入风险复核",
}


def _refresh_strategy_state() -> dict[str, dict[str, Any]]:
    strategies, warning = load_strategy_registry(STRATEGY_CONFIG_PATH)
    STRATEGIES.clear()
    STRATEGIES.update(strategies)
    global STRATEGY_CONFIG_WARNING
    STRATEGY_CONFIG_WARNING = warning
    return STRATEGIES


_refresh_strategy_state()


def _active_strategies() -> dict[str, dict[str, Any]]:
    return _refresh_strategy_state()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nested_get(value: Any, path: str, default: Any = None) -> Any:
    current: Any = value
    for part in path.split("."):
        mapping = _as_mapping(current)
        if part not in mapping:
            return default
        current = mapping[part]
    return current


def _field_label(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def _plan_action_label(plan_action: str) -> str:
    return PLAN_ACTION_LABELS.get(plan_action, PLAN_ACTION_LABELS["watch"])


def _status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "部分满足")


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "ok", "sample", "sample_mode"}
    return bool(value)


def _trust_value(source: Mapping[str, Any] | Any | None) -> float | None:
    if source is None:
        return None
    if isinstance(source, Mapping):
        for key in ("data_reliability_score", "trust_score", "score"):
            value = source.get(key)
            if value not in {None, ""}:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        nested = source.get("data_trust") or source.get("trust_assessment") or source.get("data_quality")
        if isinstance(nested, Mapping):
            return _trust_value(nested)
        return None
    for key in ("data_reliability_score", "trust_score", "score"):
        if hasattr(source, key):
            try:
                value = getattr(source, key)
                if value not in {None, ""}:
                    return float(value)
            except (TypeError, ValueError):
                continue
    nested = getattr(source, "data_trust", None) or getattr(source, "trust_assessment", None) or getattr(source, "data_quality", None)
    if nested is not None:
        return _trust_value(nested)
    return None


def _timing_blocked(timing_advice: Mapping[str, Any] | Any | None) -> bool:
    if timing_advice is None:
        return False
    if isinstance(timing_advice, Mapping):
        ready = timing_advice.get("ready_for_review")
        if ready is not None and not _truthy(ready):
            reason = str(timing_advice.get("reason") or timing_advice.get("timing_label") or timing_advice.get("display_label") or "")
            if any(token in reason for token in ("休市", "节假日", "非交易", "等待收盘", "盘前", "午休")):
                return True
            return False
        state = str(timing_advice.get("timing_state") or timing_advice.get("market_session") or timing_advice.get("session") or "")
        return state in {"wait", "paper_only", "after_close_review"} and "盘中" not in state
    ready = getattr(timing_advice, "ready_for_review", None)
    if ready is not None and not _truthy(ready):
        reason = str(
            getattr(timing_advice, "reason", "")
            or getattr(timing_advice, "timing_label", "")
            or getattr(timing_advice, "display_label", "")
            or ""
        )
        if any(token in reason for token in ("休市", "节假日", "非交易", "等待收盘", "盘前", "午休")):
            return True
        return False
    state = str(getattr(timing_advice, "timing_state", "") or getattr(timing_advice, "market_session", "") or getattr(timing_advice, "session", ""))
    return state in {"wait", "paper_only", "after_close_review"} and "盘中" not in state


def list_strategies() -> dict[str, Any]:
    strategies = _active_strategies()
    return {
        "strategies": [
            {
                "strategy_id": item.get("strategy_id", strategy_id),
                "name": item.get("name", strategy_id),
                "description": item.get("description", ""),
                "entry_label": item.get("entry_label", "虚拟建仓观察条件"),
                "exit_label": item.get("exit_label", "虚拟离场观察条件"),
                "why": list(item.get("why") or []),
                "required_indicators": list(item.get("required_indicators") or []),
                "risk_line_type": item.get("risk_line_type", "ma20"),
                "enabled": bool(item.get("enabled", True)),
                "disclaimer": item.get("disclaimer", STRATEGY_DEMO_DISCLAIMER),
                "source": item.get("source", "builtin"),
            }
            for strategy_id, item in strategies.items()
            if bool(item.get("enabled", True))
        ],
        "warning": STRATEGY_CONFIG_WARNING,
        "disclaimer": STRATEGY_DEMO_DISCLAIMER,
    }


def _strategy(strategy_name: str) -> dict[str, Any]:
    strategies = _active_strategies()
    key = str(strategy_name or "均线突破").strip()
    strategy: dict[str, Any] | None = None
    for strategy_id, item in strategies.items():
        if key in {strategy_id, str(item.get("strategy_id") or ""), str(item.get("name") or "")}:
            strategy = dict(item)
            break
    if strategy is None:
        resolved = resolve_strategy(key, STRATEGY_CONFIG_PATH)
        if str(resolved.get("strategy_warning") or ""):
            raise ValueError("未知策略。")
        strategy = dict(resolved)
    strategy.setdefault("entry_label", "虚拟建仓观察条件")
    strategy.setdefault("exit_label", "虚拟离场观察条件")
    strategy.setdefault("why", [])
    strategy.setdefault("required_indicators", [])
    strategy.setdefault("risk_line_type", "ma20")
    strategy.setdefault("strategy_id", strategy_name)
    strategy.setdefault("disclaimer", STRATEGY_DEMO_DISCLAIMER)
    return strategy


def build_strategy_explanation(
    check_type_label: str,
    matched: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    blocked_by_trust: bool,
    blocked_by_timing: bool,
) -> str:
    if blocked_by_trust:
        return "由于数据可信度不足，本次策略演示被阻断。请先查看数据健康状态。"
    if blocked_by_timing:
        return "当前市场时段不适合做本规则复核，本次策略演示仅展示规则说明。"
    if missing:
        field = str(missing[0].get("field") or missing[0].get("display_condition") or "关键字段")
        return f"由于缺少{_field_label(field)}数据，本次只能完成部分规则检查。"
    if failed:
        return f"由于{failed[0].get('display_condition', '部分条件')}尚未达到规则阈值，本次未触发{check_type_label}。"
    if matched:
        return f"该策略的{check_type_label}已全部满足。本结果只表示规则检查通过，不代表真实交易决策。"
    return "本次仅完成规则说明，尚未形成足够清晰的检查结果。"


def _feature_snapshot(stock_data: Mapping[str, Any] | Any, data_mode: str = "sample") -> dict[str, Any]:
    features = build_strategy_feature_row(dict(_as_mapping(stock_data)), data_mode=data_mode)
    features.setdefault("feature_coverage", {})
    features.setdefault("feature_notes", [])
    return features


def _plan_action(check_type: str, blocked_by_trust: bool, blocked_by_timing: bool, all_matched: bool, missing_count: int) -> str:
    if blocked_by_trust or blocked_by_timing:
        return "risk_review"
    if missing_count > 0:
        return "watch"
    if all_matched:
        return "virtual_entry_candidate" if check_type == "entry" else "virtual_exit_candidate"
    return "watch"


def _condition_results(raw: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    matched = [dict(item) for item in raw.get("matched") or [] if isinstance(item, Mapping)]
    failed = [dict(item) for item in raw.get("failed") or [] if isinstance(item, Mapping)]
    missing = [dict(item) for item in raw.get("missing") or [] if isinstance(item, Mapping)]
    return matched, failed, missing


def _coverage_level(feature_coverage: Mapping[str, Any], missing: list[dict[str, Any]]) -> str:
    values = [str(value) for value in feature_coverage.values()]
    if missing:
        return "missing_required"
    if any(value in {"demo_fallback", "derived", "fallback"} for value in values):
        return "partial"
    if any(value in {"missing", "unknown"} for value in values):
        return "partial"
    return "complete"


def _missing_fields(missing: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for item in missing:
        field = str(item.get("field") or item.get("condition") or item.get("display_condition") or item.get("explain") or "")
        if field:
            labels.append(field)
    return labels


def _missing_labels(missing: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for item in missing:
        field = str(item.get("field") or item.get("condition") or "")
        label = _field_label(field) if field else str(item.get("display_condition") or item.get("explain") or "缺失字段")
        labels.append(label)
    return labels


def _demo_fallback_labels(feature_coverage: Mapping[str, Any]) -> list[str]:
    return [f"{_field_label(str(field))}（样例补全）" for field, status in feature_coverage.items() if status == "demo_fallback"]


def _short_explanation(
    status: str,
    matched: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    blocked_by_trust: bool,
    blocked_by_timing: bool,
    feature_notes: list[str],
) -> str:
    if blocked_by_trust:
        return "数据可信度不足，本次被阻断。"
    if blocked_by_timing:
        return "当前市场时段不适合做规则复核。"
    if missing:
        return f"缺少{_field_label(str(missing[0].get('field') or missing[0].get('display_condition') or '关键字段'))}，本次只能完成部分规则检查。"
    if status == "all_matched":
        return "已满足当前策略的主要观察条件。"
    if failed:
        return f"尚不满足：{failed[0].get('display_condition', '部分条件')}"
    if feature_notes:
        return feature_notes[0]
    return "继续观察。"


def _data_date_label(features: Mapping[str, Any]) -> str:
    daily = str(features.get("daily_bar_date") or "")
    quote = str(features.get("quote_date") or "")
    freshness = str(features.get("feature_freshness_label") or "")
    if daily and quote and daily != quote:
        return f"混合数据：日线 {daily} + 盘中观察 {quote}"
    if freshness:
        return freshness
    if daily:
        return f"基于完整日线：{daily}"
    if quote:
        return f"盘中估算：{quote}"
    return "数据日期未知"


def _result_row(
    stock_data: Mapping[str, Any] | Any,
    strategy_name: str,
    *,
    trust_assessment: Mapping[str, Any] | Any | None = None,
    timing_advice: Mapping[str, Any] | Any | None = None,
    check_type: str = "entry",
) -> dict[str, Any]:
    strategy = _strategy(strategy_name)
    features = _feature_snapshot(stock_data, data_mode=str(_as_mapping(stock_data).get("data_mode") or "sample"))
    # Entry uses AND (all conditions must match); exit uses OR (any condition triggers).
    eval_mode = "any" if check_type == "exit" else "all"
    raw_check = evaluate_conditions(features, list(strategy.get(f"{check_type}_conditions") or []), mode=eval_mode)
    matched, failed, missing = _condition_results(raw_check)
    feature_coverage = dict(features.get("feature_coverage") or {})
    feature_notes = list(features.get("feature_notes") or [])
    data_reliability_score = _trust_value(trust_assessment)
    if data_reliability_score is None:
        data_reliability_score = safe_float(features.get("data_reliability_score"))
    trust_assessment_map = _as_mapping(trust_assessment)
    trust_action = str(trust_assessment_map.get("action_state") or trust_assessment_map.get("manual_confirmation_level") or "")
    blocked_by_trust = bool(trust_action == "暂不参考" or (data_reliability_score is not None and data_reliability_score < 60))
    blocked_by_timing = _timing_blocked(timing_advice)
    if data_reliability_score is None:
        blocked_by_trust = False
    if str(features.get("data_category") or "") == "混合数据" and str(features.get("data_mode") or "sample") == "sample":
        blocked_by_timing = False
    if str(features.get("data_category") or "") == "等待完整日线" and str(features.get("data_mode") or "sample") == "sample":
        blocked_by_timing = False
    if str(_as_mapping(stock_data).get("data_mode") or "").lower() == "sample":
        blocked_by_timing = False

    # For exit: triggered=True means any exit condition matched (OR logic).
    # For entry: triggered=True means all conditions matched (AND logic).
    triggered = bool(raw_check.get("triggered")) and not blocked_by_trust and not blocked_by_timing
    all_matched = triggered  # backward-compatible alias
    missing_count = len(missing)
    if blocked_by_trust or blocked_by_timing:
        status = "blocked"
    elif missing_count > 0:
        status = "missing_required"
    elif all_matched:
        status = "all_matched"
    elif failed:
        status = "partial"
    else:
        status = "partial"

    plan_action = _plan_action(check_type, blocked_by_trust, blocked_by_timing, status == "all_matched", missing_count)
    plan_action_label = _plan_action_label(plan_action)
    plan_action_note = "计划动作仅用于虚拟组合或人工确认，不会触发真实交易。"
    check_type_label = str(strategy.get("entry_label" if check_type == "entry" else "exit_label") or ("虚拟建仓观察条件" if check_type == "entry" else "虚拟离场观察条件"))
    explanation = build_strategy_explanation(check_type_label, matched, failed, missing, blocked_by_trust, blocked_by_timing)
    short_explanation = _short_explanation(status, matched, failed, missing, blocked_by_trust, blocked_by_timing, feature_notes)
    coverage_level = _coverage_level(feature_coverage, missing)
    block_reason = "数据可信度不足，策略演示被阻断。" if blocked_by_trust else ("当前市场时段不适合做本规则复核。" if blocked_by_timing else "")
    if coverage_level == "partial" and any(value == "demo_fallback" for value in feature_coverage.values()):
        explanation = "部分字段为样例演示补全，仅用于学习规则检查流程。" + explanation
    trust_payload = dict(_as_mapping(trust_assessment))
    timing_payload = dict(_as_mapping(timing_advice))
    trust_payload.pop("disclaimer", None)
    timing_payload.pop("disclaimer", None)

    return {
        "symbol": str(features.get("symbol") or _as_mapping(stock_data).get("symbol") or _as_mapping(stock_data).get("code") or "").zfill(6),
        "name": str(features.get("name") or _as_mapping(stock_data).get("name") or ""),
        "strategy_id": strategy.get("strategy_id", strategy_name),
        "strategy_name": strategy.get("name", strategy_name),
        "strategy_description": strategy.get("description", ""),
        "strategy_source": strategy.get("source", "builtin"),
        "check_type": check_type,
        "check_type_label": check_type_label,
        "matched": matched,
        "failed": failed,
        "missing": missing,
        "matched_count": len(matched),
        "failed_count": len(failed),
        "missing_count": len(missing),
        "all_matched": status == "all_matched",
        "blocked_by_trust": blocked_by_trust,
        "blocked_by_timing": blocked_by_timing,
        "block_reason": block_reason,
        "status": status,
        "status_label": _status_label(status),
        "plan_action": plan_action,
        "plan_action_label": plan_action_label,
        "plan_action_note": plan_action_note,
        "explanation": explanation,
        "short_explanation": short_explanation,
        "feature_coverage": feature_coverage,
        "feature_notes": feature_notes,
        "features": features,
        "coverage_level": coverage_level,
        "data_date": features.get("data_date"),
        "daily_bar_date": features.get("daily_bar_date"),
        "quote_date": features.get("quote_date"),
        "quote_latest_time": features.get("quote_latest_time"),
        "feature_freshness": features.get("feature_freshness"),
        "feature_freshness_label": features.get("feature_freshness_label"),
        "feature_freshness_notes": list(features.get("feature_freshness_notes") or []),
        "data_category": features.get("data_category"),
        "data_date_label": _data_date_label(features),
        "missing_fields": _missing_fields(missing),
        "missing_labels": _missing_labels(missing),
        "demo_fallback_labels": _demo_fallback_labels(feature_coverage),
        "required_checks": list(strategy.get("entry_conditions" if check_type == "entry" else "exit_conditions") or []),
        "blocking_issues": ["数据可信度不足，策略演示被阻断。"] if blocked_by_trust else (["当前市场时段不适合做本规则复核。"] if blocked_by_timing else []),
        "risk_notes": list(dict.fromkeys(list(features.get("feature_notes") or []) + list(_as_mapping(trust_assessment).get("risk_notes") or []))),
        "key_risks": list(dict.fromkeys(list(_as_mapping(trust_assessment).get("risks") or []) + list(_as_mapping(stock_data).get("risk_notes") or []))),
        "trust_assessment": trust_payload,
        "timing_advice": timing_payload,
        "confidence_note": str(_as_mapping(trust_assessment).get("confidence_note") or "策略原始分不等于可靠性；可信度评分不代表价格方向判断。"),
        "disclaimer": STRATEGY_DEMO_DISCLAIMER,
        "strategy_warning": STRATEGY_CONFIG_WARNING,
    }


def check_entry_conditions(
    stock_data: Mapping[str, Any] | Any,
    strategy_name: str,
    trust_assessment: Mapping[str, Any] | Any | None = None,
    timing_advice: Mapping[str, Any] | Any | None = None,
) -> dict[str, Any]:
    return _result_row(stock_data, strategy_name, trust_assessment=trust_assessment, timing_advice=timing_advice, check_type="entry")


def check_exit_conditions(
    holding: Mapping[str, Any] | Any,
    current_data: Mapping[str, Any] | Any,
    strategy_name: str,
    trust_assessment: Mapping[str, Any] | Any | None = None,
    timing_advice: Mapping[str, Any] | Any | None = None,
) -> dict[str, Any]:
    merged = dict(_as_mapping(holding))
    merged.update(_as_mapping(current_data))
    return _result_row(merged, strategy_name, trust_assessment=trust_assessment, timing_advice=timing_advice, check_type="exit")


def check_strategy_for_stocks(
    stocks: Any,
    strategy_name: str,
    trust_map: Mapping[str, Any] | None = None,
    timing_advice: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    trust_map = trust_map or {}
    rows = list(stocks or [])
    details: list[dict[str, Any]] = []
    counts = {"all_matched": 0, "partial": 0, "blocked": 0, "missing_required": 0}
    for stock in rows:
        symbol = str(_as_mapping(stock).get("symbol") or _as_mapping(stock).get("code") or "").zfill(6)
        trust = trust_map.get(symbol) or _as_mapping(stock).get("trust_assessment") or {}
        timing = timing_advice or _as_mapping(stock).get("timing_advice") or {}
        detail = check_entry_conditions(stock, strategy_name, trust, timing)
        status = str(detail.get("status") or "partial")
        counts[status] = counts.get(status, 0) + 1
        details.append(
            {
                "symbol": detail.get("symbol", ""),
                "name": detail.get("name", ""),
                "status": status,
                "status_label": detail.get("status_label", ""),
                "matched_count": len(detail.get("matched") or []),
                "failed_count": len(detail.get("failed") or []),
                "missing_count": len(detail.get("missing") or []),
                "blocked_by_trust": bool(detail.get("blocked_by_trust")),
                "blocked_by_timing": bool(detail.get("blocked_by_timing")),
                "short_explanation": detail.get("short_explanation", ""),
                "missing_fields": list(detail.get("missing_fields") or []),
                "missing_labels": list(detail.get("missing_labels") or []),
                "plan_action": detail.get("plan_action", "watch"),
                "plan_action_label": detail.get("plan_action_label", "继续观察"),
                "data_date_label": detail.get("data_date_label", "数据日期未知"),
                "feature_freshness": detail.get("feature_freshness"),
                "detail": detail,
            }
        )
    return {
        "strategy_name": strategy_name,
        "strategy_warning": STRATEGY_CONFIG_WARNING,
        "total": len(details),
        "summary": counts,
        "rows": details,
        "disclaimer": STRATEGY_DEMO_DISCLAIMER,
    }


def compare_stock_strategies(
    stock: Mapping[str, Any] | Any,
    strategy_names: Any | None = None,
    trust_assessment: Mapping[str, Any] | Any | None = None,
    timing_advice: Mapping[str, Any] | Any | None = None,
) -> dict[str, Any]:
    rows = []
    available = list_strategies().get("strategies", [])
    if strategy_names:
        wanted = {str(name) for name in strategy_names}
        available = [
            item for item in available
            if str(item.get("name") or "") in wanted or str(item.get("strategy_id") or "") in wanted
        ]
    for rank, item in enumerate(available, start=1):
        detail = check_entry_conditions(stock, str(item.get("strategy_id") or item.get("name") or ""), trust_assessment, timing_advice)
        rows.append(
            {
                "rank": rank,
                "strategy_id": detail.get("strategy_id", ""),
                "strategy_name": detail.get("strategy_name", ""),
                "name": detail.get("strategy_name", ""),
                "status": detail.get("status", ""),
                "status_label": detail.get("status_label", ""),
                "matched_count": len(detail.get("matched") or []),
                "failed_count": len(detail.get("failed") or []),
                "missing_count": len(detail.get("missing") or []),
                "plan_action": detail.get("plan_action", "watch"),
                "plan_action_label": detail.get("plan_action_label", "继续观察"),
                "short_explanation": detail.get("short_explanation", ""),
                "risk_notes": list(detail.get("risk_notes") or []),
                "detail": detail,
            }
        )
    return {
        "symbol": str(_as_mapping(stock).get("symbol") or _as_mapping(stock).get("code") or "").zfill(6),
        "name": str(_as_mapping(stock).get("name") or ""),
        "strategies": rows,
        "ranking_note": "排序仅用于对比不同策略的规则检查状态，不代表价格方向判断。",
        "disclaimer": STRATEGY_DEMO_DISCLAIMER,
    }


def evaluate_condition(data: Mapping[str, Any], condition: Mapping[str, Any]) -> dict[str, Any]:
    return evaluate_conditions(data, [condition])


__all__ = [
    "STRATEGY_DEMO_VERSION",
    "STRATEGY_DEMO_DISCLAIMER",
    "STRATEGIES",
    "STATUS_LABELS",
    "FIELD_LABELS",
    "PLAN_ACTION_LABELS",
    "STRATEGY_CONFIG_WARNING",
    "build_strategy_explanation",
    "check_entry_conditions",
    "check_exit_conditions",
    "check_strategy_for_stocks",
    "compare_stock_strategies",
    "evaluate_condition",
    "list_strategies",
]
