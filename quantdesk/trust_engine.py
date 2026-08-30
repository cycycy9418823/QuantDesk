from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from quantdesk.shared import safe_float

TRUST_LEVELS = {"高风险", "谨慎观察", "可观察", "条件较好", "深度复核"}
ACTION_STATES = {"暂不参考", "只观察", "虚拟验证", "条件满足后再评估", "深度复核"}
DISCLAIMER = "可信度评分不代表价格方向预测，仅用于辅助分析，不构成投资建议。"


@dataclass
class TrustAssessment:
    symbol: str
    name: str
    raw_score: float
    trust_score: float
    trust_level: str
    action_state: str
    data_reliability_score: float
    news_reliability_score: float
    trend_quality_score: float
    liquidity_score: float
    risk_distance_score: float
    capital_flow_score: float
    market_context_score: float
    information_completeness_score: float
    reasons: list[str]
    risks: list[str]
    blocking_issues: list[str]
    required_checks: list[str]
    invalidation_conditions: list[str]
    beginner_explanation: str
    confidence_note: str
    disclaimer: str
    action_confidence: str = "normal"
    manual_confirmation_level: str = "normal_review"
    risk_notes: list[str] = field(default_factory=list)
    research_notes: list[str] = field(default_factory=list)
    fundamental_notes: list[str] = field(default_factory=list)
    market_notes: list[str] = field(default_factory=list)


def extract_row_metric(row: Mapping[str, Any] | Any, keys: list[str] | tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        try:
            value = row.get(key, default)
        except AttributeError:
            value = getattr(row, key, default)
        if value is not None and value != "":
            return value
    return default


def _clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _text(row: Mapping[str, Any] | Any, keys: list[str], default: str = "") -> str:
    value = extract_row_metric(row, keys, default)
    return str(value or default)


def _raw_score(row: Mapping[str, Any] | Any) -> float:
    score = safe_float(extract_row_metric(row, ["score", "raw_score", "long_score", "board_score"], 0), 0)
    return _clamp(float(score or 0))


def _data_score(row: Mapping[str, Any] | Any, risks: list[str], blocking: list[str]) -> float:
    level = _upper(extract_row_metric(row, ["data_warning_level", "warning_level"], ""))
    freshness = _text(row, ["data_freshness_status"])
    reason = _text(row, ["data_expected_reason", "data_risk_flag", "warning_message"])
    if level == "ERROR":
        blocking.append("数据错误，暂不参考。")
        risks.append(reason or "数据错误，暂不参考。")
        return 0
    if level == "WARNING":
        risks.append("数据存在 WARNING，先核对行情日期和数据源。")
        if reason:
            risks.append(reason)
        return 45
    if freshness in {"stale", "no_data"}:
        risks.append(reason or "数据早于预期交易日，先核对数据源。")
        return 35
    if freshness == "waiting_for_quote_update":
        risks.append("今日已收盘但行情源尚未更新，先等待数据确认。")
        return 65
    if freshness == "market_holiday_using_last_trading_day":
        return 90
    if level == "INFO":
        risks.append(reason or "数据有提示信息，使用前先核对。")
        return 75
    if not level:
        risks.append("数据状态字段不完整，按保守方式处理。")
        return 60
    return 95


def _news_score(row: Mapping[str, Any] | Any, risks: list[str]) -> float:
    level = _upper(extract_row_metric(row, ["news_warning_level"], ""))
    status = _text(row, ["news_status", "news_warning_message", "news_badge", "latest_news_title"])
    status_lower = status.lower()
    if "样例模式" in status or "sample_mode" in status_lower:
        # Offline/sample mode means "not evaluated", not "failed". Because the
        # system defaults to offline, scoring it as a failure (45) permanently
        # penalised every candidate and capped trust. Treat it as neutral so a
        # strong offline candidate can still reach a high tier; genuine fetch
        # failures in the branch below still score low.
        risks.append("样例模式未联网，消息维度按中性处理（不加分不扣分），联网后再确认。")
        return 70
    if level in {"WARNING", "ERROR"} or "失败" in status or "failed" in status_lower:
        risks.append("消息信息不完整或抓取失败，先确认公告和新闻来源。")
        return 45
    if "过热" in status or "追高" in status:
        risks.append("消息或短期情绪偏热，注意追高风险。")
        return 55
    if level == "INFO" or "暂无相关新闻" in status or "缺失" in status:
        risks.append("消息信息不足，不能作为判断依据，需等待更新。")
        return 60
    if not status:
        risks.append("消息字段缺失，按保守方式处理。")
        return 60
    return 90


def _trend_score(row: Mapping[str, Any] | Any, reasons: list[str]) -> float:
    """Trend score only uses moving-average relationships, not raw strategy score."""
    close = safe_float(extract_row_metric(row, ["close", "price"], None), None)
    ma20 = safe_float(extract_row_metric(row, ["ma20"], None), None)
    ma60 = safe_float(extract_row_metric(row, ["ma60"], None), None)
    score = 50.0
    close_above_ma20 = close is not None and ma20 is not None and close > ma20
    ma20_above_ma60 = ma20 is not None and ma60 is not None and ma20 > ma60
    if close is not None and ma20 is not None:
        score += 15 if close_above_ma20 else -10
    if close_above_ma20:
        reasons.append("价格站上 MA20，短期结构相对更清晰。")
    if ma20_above_ma60:
        score += 15
        reasons.append("MA20 高于 MA60，中期趋势结构较顺。")
    if close_above_ma20 and ma20_above_ma60:
        score = min(score, 85)
    return _clamp(score, 30, 85)


def _liquidity_score(row: Mapping[str, Any] | Any, risks: list[str], reasons: list[str]) -> float:
    amount_yi = safe_float(extract_row_metric(row, ["amount_yi"], None), None)
    amount = safe_float(extract_row_metric(row, ["amount", "成交额"], None), None)
    if amount_yi is None and amount is not None:
        amount_yi = amount / 100000000
    if amount_yi is None:
        risks.append("成交额信息缺失，流动性按保守方式处理。")
        return 55
    if amount_yi < 0.5:
        risks.append("成交额偏低，流动性不足。")
        return 35
    if amount_yi < 1:
        risks.append("成交额一般，需控制观察仓位和滑点。")
        return 60
    reasons.append("成交额达到基本观察门槛。")
    return 85


def _risk_distance_score(row: Mapping[str, Any] | Any, risks: list[str], reasons: list[str]) -> float:
    risk_pct = safe_float(extract_row_metric(row, ["risk_pct", "stop_distance_pct"], None), None)
    if risk_pct is None:
        risks.append("止损距离字段缺失，容错空间不明确。")
        return 50
    if risk_pct > 12:
        risks.append("当前价格距离止损位偏远，容错空间较小。")
        return 35
    if risk_pct > 8:
        risks.append("止损距离略远，先等待更合适的观察点。")
        return 60
    reasons.append("止损距离相对可控。")
    return 85


def _capital_flow_score(row: Mapping[str, Any] | Any, risks: list[str], reasons: list[str]) -> float:
    amount_ratio = safe_float(extract_row_metric(row, ["amount_ratio"], None), None)
    change_pct = safe_float(extract_row_metric(row, ["change_pct", "涨跌幅"], 0), 0) or 0
    if change_pct >= 8:
        risks.append("短期涨幅较高，注意接近涨停或追高风险。")
    if amount_ratio is None:
        return 60
    if amount_ratio >= 1:
        reasons.append("成交活跃度相对正常。")
        return 80
    return 55


def _market_context_score(context: Mapping[str, Any] | None, risks: list[str]) -> float:
    context = context or {}
    regime = str(context.get("market_regime") or "")
    label = str(context.get("market_regime_label") or "")
    if regime == "market_strong":
        return 82
    if regime == "market_supportive":
        return 72
    if regime == "market_correction":
        risks.append(label or "市场回调中，需要更严格人工确认。")
        return 55
    if regime == "market_pressure":
        risks.append(label or "市场环境承压，计划动作需要更谨慎复核。")
        return 42
    if regime == "market_unknown":
        risks.append("市场环境数据不足，按保守方式处理。")
        return 58
    label = str(context.get("market_temperature") or context.get("market_label") or context.get("label") or "")
    score = safe_float(context.get("temp_score"), None)
    if "偏弱" in label or (score is not None and score < 45):
        risks.append("市场环境偏弱，降低观察频率。")
        return 45
    if score is not None and score >= 70:
        return 80
    return 65


def _completeness_score(row: Mapping[str, Any] | Any, risks: list[str]) -> float:
    keys = ["score", "data_warning_level", "news_status", "close", "ma20", "ma60", "risk_pct", "amount_yi"]
    present = sum(1 for key in keys if extract_row_metric(row, [key], None) not in {None, ""})
    score = present / len(keys) * 100
    if score < 70:
        risks.append("关键信息不完整，可信度自动降级。")
    return _clamp(score)


def _level_and_action(
    trust_score: float,
    data_level: str,
    data_score: float,
    news_score: float,
    risk_distance_score: float,
    liquidity_score: float,
    market_context_score: float,
    risks: list[str],
) -> tuple[str, str]:
    if data_level == "ERROR" or data_score <= 10:
        return "高风险", "暂不参考"
    if data_level == "WARNING":
        return "谨慎观察", "只观察" if trust_score < 55 else "虚拟验证"
    if news_score <= 50:
        return "谨慎观察", "虚拟验证"
    if risk_distance_score < 50 or liquidity_score < 50:
        return "谨慎观察", "只观察"
    if market_context_score < 50:
        return "谨慎观察", "只观察"
    if trust_score >= 82 and not risks and data_score >= 85 and risk_distance_score >= 80 and liquidity_score >= 75 and news_score >= 70:
        return "深度复核", "深度复核"
    if trust_score >= 76 and data_score >= 85 and risk_distance_score >= 80 and liquidity_score >= 75 and news_score >= 70:
        return "条件较好", "条件满足后再评估"
    if trust_score >= 62:
        return "可观察", "虚拟验证"
    return "谨慎观察", "只观察"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _context_sources(
    context: Mapping[str, Any] | None,
    market_regime: Mapping[str, Any] | None,
    fundamental_health: Mapping[str, Any] | None,
    research_summary: Mapping[str, Any] | None,
    paper_performance_summary: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    context = context or {}
    market = market_regime or _as_mapping(context.get("market_regime") or context.get("market_regime_snapshot"))
    fundamental = fundamental_health or _as_mapping(context.get("fundamental_health") or context.get("fundamental_health_summary"))
    research = research_summary or _as_mapping(context.get("research_summary") or context.get("research_metrics"))
    paper = paper_performance_summary or _as_mapping(context.get("paper_performance_summary") or context.get("paper_review"))
    return context, _as_mapping(market), _as_mapping(fundamental), _as_mapping(research), _as_mapping(paper)


def assess_stock_trust(
    row: Mapping[str, Any] | Any,
    context: Mapping[str, Any] | None = None,
    *,
    market_regime: Mapping[str, Any] | None = None,
    fundamental_health: Mapping[str, Any] | None = None,
    research_summary: Mapping[str, Any] | None = None,
    paper_performance_summary: Mapping[str, Any] | None = None,
) -> TrustAssessment:
    reasons: list[str] = []
    risks: list[str] = []
    blocking: list[str] = []
    context, market, fundamental, research, paper = _context_sources(
        context,
        market_regime,
        fundamental_health,
        research_summary,
        paper_performance_summary,
    )
    market_notes: list[str] = []
    fundamental_notes: list[str] = []
    research_notes: list[str] = []
    paper_notes: list[str] = []
    raw_score = _raw_score(row)
    data_score = _data_score(row, risks, blocking)
    news_score = _news_score(row, risks)
    trend_score = _trend_score(row, reasons)
    liquidity_score = _liquidity_score(row, risks, reasons)
    risk_distance_score = _risk_distance_score(row, risks, reasons)
    capital_flow_score = _capital_flow_score(row, risks, reasons)
    market_context_score = _market_context_score(context, risks)
    completeness_score = _completeness_score(row, risks)

    market_regime_code = str(market.get("market_regime") or context.get("market_regime") or "")
    if market_regime_code in {"market_pressure", "market_correction"}:
        note = str(market.get("explanation") or market.get("market_regime_label") or "市场环境承压，计划动作需要更谨慎复核。")
        market_notes.append(note)
        risks.append(note)
        market_context_score = min(market_context_score, 50 if market_regime_code == "market_pressure" else 58)
    elif market_regime_code == "market_unknown":
        market_notes.append("市场环境数据不足。")

    fundamental_status = str(fundamental.get("fundamental_status") or fundamental.get("status") or "")
    if fundamental_status == "red_flag":
        note = "财务红线触发，需要优先风险复核。"
        fundamental_notes.append(note)
        risks.append(note)
        blocking.append("财务红线触发，暂不进入计划动作。")
        data_score = min(data_score, 65)
    elif fundamental_status in {"warning", "missing", "unknown"}:
        fundamental_notes.append(str(fundamental.get("explanation") or "财务信息不完整，需要人工核对。"))
    elif fundamental_status == "provider_unavailable":
        fundamental_notes.append("财务数据源未启用，本次不作为硬阻断。")

    sample_count = safe_float(research.get("sample_count"), None)
    if sample_count is not None and sample_count < 20:
        research_notes.append("历史样本数量较少，研究统计只能作为参考。")
        risks.append("历史样本不足，可信度按保守方式处理。")
        completeness_score = min(completeness_score, 75)
    elif research.get("survivorship_bias_warning"):
        research_notes.append("当前历史样本可能存在幸存者偏差。")

    recent_virtual_return = safe_float(paper.get("recent_virtual_return") or paper.get("total_pnl_pct"), None)
    if recent_virtual_return is not None and recent_virtual_return < -0.05:
        paper_notes.append("该策略近期虚拟表现不稳定，需要风险复核。")
        risks.append("虚拟组合近期表现不稳定。")

    trust_score = (
        data_score * 0.23
        + news_score * 0.12
        + trend_score * 0.19
        + liquidity_score * 0.12
        + risk_distance_score * 0.14
        + capital_flow_score * 0.08
        + market_context_score * 0.06
        + completeness_score * 0.06
    )
    if data_score < 50:
        trust_score = min(trust_score, 55)
    if news_score <= 45:
        trust_score = min(trust_score, 65)
    if risk_distance_score < 50:
        trust_score = min(trust_score, 62)
    if completeness_score < 70:
        trust_score = min(trust_score, 60)
    if fundamental_status == "red_flag":
        trust_score = min(trust_score, 50)
    if market_regime_code == "market_pressure":
        trust_score = min(trust_score, 68)

    data_level = _upper(extract_row_metric(row, ["data_warning_level", "warning_level"], ""))
    trust_level, action_state = _level_and_action(
        trust_score,
        data_level,
        data_score,
        news_score,
        risk_distance_score,
        liquidity_score,
        market_context_score,
        risks,
    )
    required_checks = [
        "先核对行情日期、数据源和 Data Health。",
        "确认消息状态和公告原文。",
        "检查价格与历史波动区间的距离。",
        "确认成交额处于历史正常范围。",
        "收盘后重新复核趋势和虚拟持仓记录。",
    ]
    invalidation_conditions = [
        "数据变为 ERROR 或行情日期早于预期交易日。",
        "收盘跌破 MA20 或 MA60。",
        "出现放量长阴或消息风险扩大。",
        "市场环境明显转弱。",
        "回测假设不适用于当前流动性或涨跌停状态。",
    ]
    if blocking:
        beginner = "当前主要问题是数据或信息可靠性不足，先暂不参考，等数据修复后再复核。"
    elif action_state == "只观察":
        beginner = "这只股票可以放在观察列表，但现在还缺少足够可靠的确认条件。"
    elif action_state == "虚拟验证":
        beginner = "可以先用虚拟持仓观察它是否按计划运行，不要把它当成真实收益判断。"
    elif action_state == "条件满足后再评估":
        beginner = "条件相对清楚，但仍要先完成数据、消息、风险距离和市场环境复核。"
    else:
        beginner = "条件较完整，但仍只表示进入深度复核清单，不代表可以省略风险检查。"

    if fundamental_status == "red_flag":
        manual_confirmation_level = "risk_review"
        action_confidence = "risk_review"
    elif action_state == "暂不参考" or blocking:
        manual_confirmation_level = "blocked"
        action_confidence = "blocked"
    elif market_regime_code in {"market_pressure", "market_correction"}:
        manual_confirmation_level = "strict_review"
        action_confidence = "strict"
    else:
        manual_confirmation_level = "normal_review"
        action_confidence = "normal"

    return TrustAssessment(
        symbol=str(extract_row_metric(row, ["code", "symbol"], "")).zfill(6),
        name=str(extract_row_metric(row, ["name"], "")),
        raw_score=round(raw_score, 2),
        trust_score=round(_clamp(trust_score), 2),
        trust_level=trust_level,
        action_state=action_state,
        data_reliability_score=round(data_score, 2),
        news_reliability_score=round(news_score, 2),
        trend_quality_score=round(trend_score, 2),
        liquidity_score=round(liquidity_score, 2),
        risk_distance_score=round(risk_distance_score, 2),
        capital_flow_score=round(capital_flow_score, 2),
        market_context_score=round(market_context_score, 2),
        information_completeness_score=round(completeness_score, 2),
        reasons=reasons[:5] or ["暂未形成足够清晰的可信理由。"],
        risks=list(dict.fromkeys(risks))[:6],
        blocking_issues=blocking,
        required_checks=required_checks,
        invalidation_conditions=invalidation_conditions,
        beginner_explanation=beginner,
        confidence_note="策略原始分不等于可靠性；可信度评分不代表价格方向预测；进入深度复核状态不表示建议任何仓位，仅说明数据与规则条件相对完整。",
        disclaimer=DISCLAIMER,
        action_confidence=action_confidence,
        manual_confirmation_level=manual_confirmation_level,
        risk_notes=list(dict.fromkeys(risks + paper_notes))[:8],
        research_notes=research_notes,
        fundamental_notes=fundamental_notes,
        market_notes=market_notes,
    )


def assess_many_stocks(rows: list[Mapping[str, Any]] | Any, context: Mapping[str, Any] | None = None) -> list[TrustAssessment]:
    if hasattr(rows, "to_dict"):
        iterable = rows.to_dict("records")
    else:
        iterable = rows or []
    return [assess_stock_trust(row, context) for row in iterable]


def trust_assessment_to_dict(assessment: TrustAssessment) -> dict[str, Any]:
    data = asdict(assessment)
    data["risk_notes"] = data.get("risk_notes") or []
    data["research_notes"] = data.get("research_notes") or []
    data["fundamental_notes"] = data.get("fundamental_notes") or []
    data["market_notes"] = data.get("market_notes") or []
    data["reliability_breakdown"] = {
        "数据可信度": assessment.data_reliability_score,
        "消息可信度": assessment.news_reliability_score,
        "趋势质量": assessment.trend_quality_score,
        "流动性": assessment.liquidity_score,
        "风险距离": assessment.risk_distance_score,
        "资金活跃度": assessment.capital_flow_score,
        "市场环境": assessment.market_context_score,
        "信息完整度": assessment.information_completeness_score,
    }
    return data
