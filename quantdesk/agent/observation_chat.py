"""Interactive, evidence-grounded observation chat.

The existing ``agent_safe_bridge.generate_llm_observation_report`` is a one-shot
report generator. This module adds **multi-turn chat** on top of the same safety
stack: the model only sees the read-only evidence pack, every reply is run
through the policy guard (advice-language removal) and the hallucination guard
(unsupported numbers / dates / symbols), and the whole thing degrades to a safe
message when the LLM is disabled, offline, or unconfigured.

Optional ``.md`` skills under ``prompts/agent_skills/`` customise the report
shape (daily review, candidate explainer, risk audit, ...).

Local research only; not investment advice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack, build_evidence_summary_for_prompt
from quantdesk.agent.agent_hallucination_guard import validate_report_grounding
from quantdesk.agent.agent_policy_guard import detect_forbidden_trading_advice, sanitize_forbidden_trading_terms
from quantdesk.agent.agent_safe_bridge import build_agent_system_rules, select_llm_provider
from quantdesk.llm_config import get_llm_provider_config, is_llm_enabled, load_llm_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = PROJECT_ROOT / "prompts" / "agent_skills"
DISCLAIMER = "对话仅基于本地只读证据用于学习复盘，不构成投资建议，也不预测价格。"

_PAPER_DECISION_FIELD_ALIASES = {
    "decision_status": "decision_status",
    "primary_action": "primary_action",
    "confidence": "confidence",
    "validation_days": "validation_days",
    "success_condition_1": "success_condition_1",
    "failure_condition_1": "failure_condition_1",
    "missing_data": "missing_data",
    "review_sentence": "review_sentence",
}

_CONTEXT_KEYS = (
    "symbol", "date", "data_health", "strategy_outputs", "intraday_status",
    "paper_portfolio_status", "provider_chain", "validation_summary",
    "feature_quality_summary", "statistical_validation_summary", "data_gaps",
    "chanlun", "warnings", "unified_ranking", "trust_assessment",
    "backtest_summary", "factor_health", "allowed_agent_actions",
    "forbidden_agent_actions",
    "target_date", "latest_local_date", "workflow_status", "data_freshness",
    "candidate_count", "current_ui_candidate_count", "today_recommendation_count",
    "observation_candidate_count", "candidate_strategy_counts",
    "candidate_data_status_counts", "candidates_preview", "top_priority_candidates",
    "paper_simulation_watchlist_count", "paper_simulation_preview",
    "paper_simulation_summary", "paper_simulation_boundary",
    "strategy_scorecard_summary", "market_background",
    "focused_candidate", "focused_candidate_locator",
)


def list_skills() -> list[str]:
    """Return available skill names (``.md`` stems under prompts/agent_skills)."""
    if not SKILLS_DIR.exists():
        return []
    return sorted(path.stem for path in SKILLS_DIR.glob("*.md"))


def load_skill(name: str | None) -> str:
    """Return a skill's markdown text, or "" if absent. Path-traversal safe."""
    if not name:
        return ""
    candidate = (SKILLS_DIR / f"{name}.md").resolve()
    try:
        candidate.relative_to(SKILLS_DIR.resolve())
    except ValueError:
        return ""
    if not candidate.exists():
        return ""
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return ""


def parse_paper_decision_fields(reply: str) -> dict[str, Any]:
    """Extract the machine-readable paper-pool decision block from an LLM reply.

    The skill asks the model to append ``key: value`` lines. This parser is
    deliberately forgiving and returns an empty dict when the block is absent.
    The parsed result is still only a user-confirmed paper-research action, not
    trading advice or an automatic execution instruction.
    """
    text = str(reply or "")
    if "纸面池决策字段" not in text and "decision_status" not in text:
        return {}
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("`").strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        clean_key = key.strip().lower().replace(" ", "_")
        canonical = _PAPER_DECISION_FIELD_ALIASES.get(clean_key)
        if not canonical:
            continue
        clean_value = value.strip().strip("`").strip()
        if clean_value:
            fields[canonical] = clean_value[:600]
    if not fields:
        return {}
    decision_status = fields.get("decision_status", "")
    lifecycle_action = "observe"
    if "进入纸面模拟池" in decision_status or decision_status.startswith("建议进入"):
        lifecycle_action = "add_to_paper"
    elif "降级复盘" in decision_status:
        lifecycle_action = "review"
    elif "降级观察" in decision_status or "暂缓" in decision_status or "继续" in decision_status or "留在纸面" in decision_status:
        lifecycle_action = "continue"
    elif "归档" in decision_status:
        lifecycle_action = "archive"

    validation_days_raw = fields.get("validation_days", "")
    validation_days: int | None = None
    match = re.search(r"\d+", validation_days_raw)
    if match:
        validation_days = int(match.group(0))

    return {
        "available": True,
        "decision_status": decision_status or "数据缺失",
        "primary_action": fields.get("primary_action") or "观察讨论",
        "confidence": fields.get("confidence") or "数据缺失",
        "validation_days": validation_days,
        "validation_days_raw": validation_days_raw or "数据缺失",
        "success_condition": fields.get("success_condition_1") or "数据缺失",
        "failure_condition": fields.get("failure_condition_1") or "数据缺失",
        "missing_data": fields.get("missing_data") or "数据缺失",
        "review_sentence": fields.get("review_sentence") or "数据缺失",
        "recommended_lifecycle_action": lifecycle_action,
        "action_label": {
            "add_to_paper": "确认加入纸面模拟池",
            "review": "确认降级复盘",
            "continue": "继续纸面验证/观察",
            "archive": "归档保留",
            "observe": "仅观察讨论",
        }.get(lifecycle_action, "仅观察讨论"),
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "not_position_advice": True,
    }


def build_chat_messages(
    question: str,
    context: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    *,
    skill_text: str = "",
) -> list[dict[str, str]]:
    """Assemble OpenAI-style messages: system rules (+skill), read-only evidence,
    prior turns, then the user's question."""
    system = build_agent_system_rules()
    if skill_text.strip():
        system = f"{system}\n\n# 本次对话技能说明（仍受上述只读与无建议约束）\n{skill_text.strip()}"

    evidence_summary = build_evidence_summary_for_prompt(context)
    compact = {key: context.get(key) for key in _CONTEXT_KEYS if context.get(key) not in (None, "")}
    evidence_message = (
        "只读证据（必须仅依据这里的事实回答；缺失就直接说缺失；"
        "不要补充外部行情/新闻/预测，不要编造价格、收益或代码）：\n"
        f"{evidence_summary}\n\n"
        + json.dumps(compact, ensure_ascii=False, indent=2, default=str)
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "system", "content": evidence_message},
    ]
    for turn in history or []:
        role = str(turn.get("role") or "")
        content = turn.get("content")
        if role in {"user", "assistant"} and content not in (None, ""):
            messages.append({"role": role, "content": str(content)})
    messages.append({"role": "user", "content": str(question or "")})
    return messages


def _safe(reply: str, *, status: str, ok: bool, provider: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "ok": ok,
        "status": status,
        "reply": reply,
        "llm_used": False,
        "provider": provider,
        "policy_guard_status": "not_checked",
        "disclaimer": DISCLAIMER,
    }
    payload.update(extra)
    return payload


def _local_safe_reply(
    question: str,
    context: dict[str, Any],
    *,
    status_reason: str,
    status: str,
    provider: str,
) -> dict[str, Any]:
    reply = build_local_observation_reply(question, context, status_reason=status_reason)
    return _safe(
        reply,
        status=status,
        ok=True,
        provider=provider,
        local_analysis=True,
        paper_decision=parse_paper_decision_fields(reply),
    )


def _count_text(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "无"
    return "，".join(f"{key} {val}" for key, val in value.items())


def _candidate_line(item: dict[str, Any]) -> str:
    rank = item.get("priority_rank") or item.get("priority_label") or "-"
    name = item.get("name") or "名称缺失"
    symbol = item.get("symbol") or "代码缺失"
    lane = item.get("strategy_lane") or item.get("strategy_bucket") or "策略分层缺失"
    data_status = item.get("data_status") or "数据状态缺失"
    risk = item.get("risk_level") or "风险层级缺失"
    reason = str(item.get("reason") or "").strip()
    reason_text = f"；证据：{reason[:120]}{'...' if len(reason) > 120 else ''}" if reason else ""
    return f"- #{rank} {name}（{symbol}）：{lane}，{data_status}，风险 {risk}{reason_text}"


def _short_text(value: Any, fallback: str = "数据缺失", *, max_len: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or text.lower() == "nan":
        return fallback
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _focused_data_gap_text(focused: dict[str, Any]) -> str:
    gaps: list[str] = []
    quality = _as_dict(_as_dict(focused.get("strategy_review")).get("data_quality"))
    for item in _as_list(quality.get("missing_reasons"))[:4]:
        text = _short_text(item, "", max_len=90)
        if text:
            gaps.append(text)
    news = _as_dict(focused.get("news_summary"))
    if news and not news.get("available"):
        gaps.append(news.get("hint") or "新闻线索缺失")
    industry = _as_dict(focused.get("industry_comparison"))
    if industry and not industry.get("available"):
        gaps.append(industry.get("hint") or "行业对比缺失")
    tracking = _as_dict(focused.get("tracking_vs_benchmark"))
    if tracking and not tracking.get("available"):
        gaps.append(tracking.get("hint") or "入选以来指数对比缺失")
    reference = _as_dict(focused.get("research_reference"))
    if reference and not reference.get("available"):
        gaps.append(reference.get("message") or "研究情景缺失")
    return "；".join(dict.fromkeys(_short_text(item, "", max_len=120) for item in gaps if item)) or "暂无关键缺口"


def _focused_success_failure(focused: dict[str, Any], primary_action: str, decision_status: str) -> tuple[str, str, int]:
    contract = _as_dict(focused.get("paper_research_contract"))
    review = _as_dict(focused.get("strategy_review"))
    tracking = _as_dict(review.get("tracking"))
    projection = _as_dict(_as_dict(focused.get("research_reference")).get("scenario_projection"))
    success_items = [str(item) for item in _as_list(contract.get("success_conditions")) if item]
    failure_items = [str(item) for item in _as_list(contract.get("failure_conditions")) if item]
    cycle = _as_dict(contract.get("validation_cycle"))
    validation_days = 3 if "打板" in primary_action else 10 if "长期" in primary_action or "波段" in primary_action else 5
    try:
        validation_days = int(cycle.get("review_trading_days") or validation_days)
    except (TypeError, ValueError):
        pass

    if "降级复盘" in decision_status:
        success = "复盘能解释原入选假设哪里失效，并把同类条件沉淀进策略成绩单。"
        failure = _short_text(tracking.get("review_note") or (failure_items[0] if failure_items else ""), max_len=160)
        if failure == "数据缺失":
            failure = "继续偏离原观察路径，且数据/新闻/行业证据无法支持原逻辑。"
        return success, failure, max(1, min(validation_days, 3))

    scenario_items = _as_list(projection.get("scenarios"))
    trigger = ""
    for item in scenario_items:
        if isinstance(item, dict) and str(item.get("tone") or "") in {"strong", "neutral"}:
            trigger = _short_text(item.get("trigger"), "", max_len=140)
            break
    success = _short_text(success_items[0] if success_items else trigger, max_len=180)
    if success == "数据缺失":
        success = "后续完整日线继续支持入选理由，且跟踪状态不转为偏离观察假设。"
    failure = _short_text(failure_items[0] if failure_items else tracking.get("review_note"), max_len=180)
    if failure == "数据缺失":
        failure = "入选后表现与原始预期持续相反，或关键数据缺口使样本无法验证。"
    return success, failure, validation_days


def _focused_candidate_decision(focused: dict[str, Any]) -> dict[str, Any]:
    basic = _as_dict(focused.get("basic"))
    candidate = _as_dict(focused.get("candidate"))
    review = _as_dict(focused.get("strategy_review"))
    tracking = _as_dict(review.get("tracking"))
    quality = _as_dict(review.get("data_quality"))
    paper = _as_dict(focused.get("paper_simulation"))
    reference = _as_dict(focused.get("research_reference"))
    projection = _as_dict(reference.get("scenario_projection"))
    news = _as_dict(focused.get("news_summary"))
    industry = _as_dict(focused.get("industry_comparison"))
    tracking_vs_benchmark = _as_dict(focused.get("tracking_vs_benchmark"))

    strategy_lane = _short_text(review.get("strategy_lane") or candidate.get("strategy_lane") or candidate.get("strategy_bucket"), "观察讨论", max_len=80)
    primary_action = strategy_lane
    if "打板" in strategy_lane:
        primary_action = "打板复核"
    elif "波段" in strategy_lane:
        primary_action = "长期-波段复核"
    elif "长期" in strategy_lane:
        primary_action = "长期-只观察"
    elif "复盘" in strategy_lane:
        primary_action = "降级复盘"

    tracking_label = _short_text(tracking.get("status_label"), "数据不足", max_len=80)
    tracking_tone = str(tracking.get("status_tone") or "")
    quality_tone = str(quality.get("tone") or "")
    data_gap_text = _focused_data_gap_text(focused)
    severe_gap = quality_tone in {"missing", "error"} or "日线缺失" in data_gap_text or "研究情景缺失" in data_gap_text
    in_paper = bool(paper.get("in_paper_pool"))
    today_focus = bool(candidate.get("is_today_recommendation"))
    has_news = bool(news.get("available"))
    has_industry = bool(industry.get("available"))
    has_benchmark = bool(tracking_vs_benchmark.get("available"))
    bias = _short_text(projection.get("bias"), "", max_len=80)

    if "偏离" in tracking_label or tracking_tone == "warn":
        decision_status = "进入降级复盘"
        primary_action = "降级复盘"
        confidence = "高" if not severe_gap else "中"
        judgment = "我更倾向先把它当作偏离样本复盘，而不是继续放在重点推进位。核心原因是跟踪结果已经和原始观察假设出现冲突，这类样本最有价值的是反推策略哪里误判。"
    elif severe_gap:
        decision_status = "暂缓进入"
        confidence = "中"
        judgment = "我不会急着让它进纸面池。不是因为标的完全没价值，而是关键证据还不够完整；在纸面池里验证一个证据缺口很大的样本，会污染后面的策略成绩单。"
    elif in_paper:
        decision_status = "继续纸面验证"
        confidence = "中" if data_gap_text != "暂无关键缺口" else "高"
        judgment = "它已经在纸面模拟池里，当前更重要的是按原验证周期继续盯结果，而不是重新给它贴候选标签。接下来重点看表现是否继续贴合入选时的路径。"
    elif today_focus or "偏强" in bias or tracking_tone == "ok":
        decision_status = "建议进入纸面模拟池"
        confidence = "高" if has_industry and has_benchmark and data_gap_text == "暂无关键缺口" else "中"
        judgment = "我更倾向把它放进纸面模拟池验证。理由不是“看起来会涨”，而是它已经具备明确入选理由、跟踪路径和可观察失败条件，适合作为策略样本而不是只停留在候选池。"
    else:
        decision_status = "暂缓进入"
        confidence = "中"
        judgment = "我会先把它放在观察/讨论区，不急着进纸面池。当前证据还没有强到值得占用验证名额，先等后续日线、行业或新闻线索给出更清楚的方向。"

    success, failure, validation_days = _focused_success_failure(focused, primary_action, decision_status)
    bullish: list[str] = []
    bearish: list[str] = []
    reason = _short_text(review.get("select_reason") or candidate.get("reason"), "", max_len=180)
    if reason:
        bullish.append(f"入选理由：{reason}")
    if bias:
        bullish.append(f"情景推演：{bias}，{_short_text(projection.get('thesis'), '', max_len=160)}")
    if has_industry:
        bullish.append(
            f"行业对比：{industry.get('sector_name', '行业缺失')}，个股相对行业 {industry.get('stock_vs_sector', '数据缺失')}。"
        )
    if has_news:
        headlines = _as_list(news.get("headlines"))
        title = _short_text(_as_dict(headlines[0]).get("title") if headlines else "", "", max_len=100)
        bullish.append(f"新闻线索：{title or '已有新闻缓存'}")
    if has_benchmark:
        bullish.append(f"入选以来对比：{_short_text(tracking_vs_benchmark.get('verdict'), max_len=150)}")

    if tracking_label != "数据不足":
        (bearish if "偏离" in tracking_label else bullish).append(f"跟踪状态：{tracking_label}。")
    if data_gap_text != "暂无关键缺口":
        bearish.append(f"证据缺口：{data_gap_text}")
    risk_level = _short_text(basic.get("risk_level") or candidate.get("risk_level"), "", max_len=40)
    if risk_level and risk_level != "数据缺失":
        bearish.append(f"风险层级：{risk_level}。")
    if not bearish:
        bearish.append("最大的反方证据不是单点风险，而是后续完整日线不能继续兑现入选理由。")

    return {
        "decision_status": decision_status,
        "primary_action": primary_action,
        "confidence": confidence,
        "validation_days": validation_days,
        "success_condition": success,
        "failure_condition": failure,
        "missing_data": data_gap_text,
        "review_sentence": (
            "优先进入纸面池验证原始策略假设。"
            if decision_status == "建议进入纸面模拟池"
            else "继续按纸面合同验证，不新增真实交易含义。"
            if decision_status == "继续纸面验证"
            else "先复盘偏离原因，再决定是否保留为观察样本。"
            if decision_status == "进入降级复盘"
            else "先补证据和观察，不急着进入纸面验证。"
        ),
        "judgment": judgment,
        "bullish": bullish[:5] or ["当前缺少足够强的正向证据。"],
        "bearish": bearish[:5],
    }


def _build_focused_candidate_reply(question: str, context: dict[str, Any], *, status_reason: str = "") -> str:
    focused = _as_dict(context.get("focused_candidate"))
    basic = _as_dict(focused.get("basic"))
    review = _as_dict(focused.get("strategy_review"))
    tracking = _as_dict(review.get("tracking"))
    reference = _as_dict(focused.get("research_reference"))
    projection = _as_dict(reference.get("scenario_projection"))
    technical = _as_dict(focused.get("technical_indicators"))
    decision = _focused_candidate_decision(focused)
    name = _short_text(basic.get("name") or _as_dict(focused.get("locator")).get("name"), "名称缺失", max_len=80)
    symbol = _short_text(basic.get("symbol") or context.get("symbol"), "代码缺失", max_len=20)
    latest_date = _short_text(basic.get("latest_date") or context.get("date") or context.get("target_date"), "数据缺失", max_len=30)
    price = _short_text(basic.get("current_price"), "数据缺失", max_len=40)
    pct = _short_text(basic.get("pct_chg"), "数据缺失", max_len=40)
    strategy = _short_text(review.get("strategy_lane") or review.get("strategy_name"), "策略分层缺失", max_len=80)
    select_reason = _short_text(review.get("select_reason"), "入选理由缺失", max_len=180)
    tracking_label = _short_text(tracking.get("status_label"), "数据不足", max_len=80)
    tracking_note = _short_text(tracking.get("review_note"), "跟踪备注缺失", max_len=180)
    scenario_bias = _short_text(projection.get("bias"), "情景缺失", max_len=80)
    scenario_thesis = _short_text(projection.get("thesis"), "情景推演缺失", max_len=180)

    lines = [
        "本次先使用本地证据做离线单股研究，未调用外部 LLM/API。",
        f"问题：{question or '未提供具体问题'}",
        f"标的：{name}（{symbol}）；数据日期：{latest_date}；当前价：{price}；涨跌幅：{pct}。",
        f"研究结论：{decision['decision_status']}（纸面研究结论，不是交易指令）。",
        f"主动作：{decision['primary_action']}；置信度：{decision['confidence']}；验证周期：{decision['validation_days']} 个完整交易日。",
        f"我的判断：{decision['judgment']}",
        f"入选逻辑：{strategy}；{select_reason}",
        f"技术证据：MACD={_short_text(technical.get('macd'), max_len=120)}；KDJ={_short_text(technical.get('kdj'), max_len=100)}；RSI={_short_text(technical.get('rsi'), max_len=80)}。",
        f"情景推演：{scenario_bias}；{scenario_thesis}",
        f"当前跟踪：{tracking_label}；{tracking_note}",
        "看好的理由：",
        *[f"- {item}" for item in decision["bullish"]],
        "反方理由：",
        *[f"- {item}" for item in decision["bearish"]],
        f"成功条件：{decision['success_condition']}",
        f"失败条件：{decision['failure_condition']}",
        f"需要补的数据：{decision['missing_data']}",
        f"一句话复盘口径：{decision['review_sentence']}",
    ]
    if status_reason:
        lines.append(f"外部 LLM 未使用原因：{status_reason}")
    lines.extend(
        [
            "纸面池决策字段:",
            f"decision_status: {decision['decision_status']}",
            f"primary_action: {decision['primary_action']}",
            f"confidence: {decision['confidence']}",
            f"validation_days: {decision['validation_days']}",
            f"success_condition_1: {decision['success_condition']}",
            f"failure_condition_1: {decision['failure_condition']}",
            f"missing_data: {decision['missing_data']}",
            f"review_sentence: {decision['review_sentence']}",
            "边界：Research Only / Paper Account Only / No Broker Route / Not Investment Advice / Not Position Advice。",
        ]
    )
    return "\n".join(lines)


def _is_simple_greeting(question: str) -> bool:
    text = str(question or "").strip().lower()
    compact = "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return compact in {"hi", "hello", "hey", "你好", "您好", "哈喽", "嗨"}


def build_local_observation_reply(question: str, context: dict[str, Any], *, status_reason: str = "") -> str:
    """Deterministic local analysis when the external LLM is unavailable."""
    if isinstance(context.get("focused_candidate"), dict) and not _is_simple_greeting(question):
        return _build_focused_candidate_reply(question, context, status_reason=status_reason)

    candidates = [
        item
        for item in (context.get("top_priority_candidates") or context.get("candidates_preview") or [])
        if isinstance(item, dict)
    ]
    candidate_count = context.get("candidate_count") or context.get("current_ui_candidate_count") or len(candidates)
    today_count = context.get("today_recommendation_count", "数据缺失")
    observation_count = context.get("observation_candidate_count", "数据缺失")
    paper_count = context.get("paper_simulation_watchlist_count", 0)
    paper_preview = [
        item
        for item in (context.get("paper_simulation_preview") or [])
        if isinstance(item, dict)
    ]
    if _is_simple_greeting(question):
        lines = [
            "你好，我是 QuantDesk 研究助手。",
            f"我已经读取到本地候选池：共 {candidate_count} 只，今日推荐 {today_count} 只，历史/观察 {observation_count} 只。",
            f"纸面模拟池：{paper_count} 只；这些样本只用于本地学习跟踪，不接券商、不下单。",
            "你可以直接问我：今天最值得复盘的是哪几只、某只股票为什么入选、哪些候选需要降级复盘、或者当前数据缺口在哪里。",
        ]
        if status_reason:
            lines.append(f"外部 LLM 未使用原因：{status_reason}")
        lines.append("边界：Research Only / Paper Account Only / No Broker Route / Not Investment Advice / Not Position Advice。")
        return "\n".join(lines)

    lines = [
        "本次先使用本地证据做离线分析，未调用外部 LLM/API。",
        f"问题：{question or '未提供具体问题'}",
        f"数据日期：{context.get('target_date') or context.get('latest_local_date') or '数据缺失'}；工作流状态：{context.get('workflow_status') or '数据缺失'}。",
        f"当前候选池：共 {candidate_count} 只；今日推荐 {today_count} 只；历史/观察 {observation_count} 只。",
        f"纸面模拟池：{paper_count} 只；只做本地跟踪复盘，不接券商、不下真实订单。",
        f"策略分层：{_count_text(context.get('candidate_strategy_counts'))}。",
        f"数据状态：{_count_text(context.get('candidate_data_status_counts'))}。",
    ]
    warnings = context.get("warnings") if isinstance(context.get("warnings"), list) else []
    if warnings:
        lines.append("需要优先确认的告警：" + "；".join(str(item) for item in warnings[:4]))
    if candidates:
        lines.append("优先复盘方向：")
        lines.extend(_candidate_line(item) for item in candidates[:5])
    else:
        lines.append("当前没有可读候选预览；请先刷新每日候选或检查本地结果文件。")
    if paper_preview:
        lines.append("纸面模拟池复盘线索：")
        for item in paper_preview[:5]:
            name = item.get("name") or "名称缺失"
            symbol = item.get("symbol") or "代码缺失"
            label = item.get("review_label") or item.get("review_status") or "跟踪中"
            ret = item.get("return_since_added_pct", "数据缺失")
            drawdown = item.get("max_drawdown_since_added", "数据缺失")
            lines.append(f"- {name}（{symbol}）：{label}；入池以来 {ret}%；最大回撤 {drawdown}%。")
    lines.append("学习复盘建议：先看数据状态是否完整，再按策略分层复核入选理由、入选后跟踪状态和风险提示。")
    if status_reason:
        lines.append(f"外部 LLM 未使用原因：{status_reason}")
    lines.append("边界：Research Only / Paper Account Only / No Broker Route / Not Investment Advice / Not Position Advice。")
    return "\n".join(lines)


def observation_chat(
    question: str,
    context: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    *,
    llm_config: dict[str, Any] | None = None,
    network: bool = False,
    skill: str | None = None,
) -> dict[str, Any]:
    """Run one grounded, guarded chat turn. Never raises; degrades to a safe
    message when disabled / offline / unconfigured / on error."""
    context = context or {}
    config = llm_config or load_llm_config()
    provider = select_llm_provider(config)

    if not is_llm_enabled(config):
        return _local_safe_reply(
            question,
            context,
            status_reason="LLM 未启用（llm_enabled=false）。",
            status="local_fallback_disabled",
            provider=provider,
        )
    if not network:
        return _local_safe_reply(
            question,
            context,
            status_reason="network=false，未显式允许调用外部 LLM/API。",
            status="local_fallback_offline",
            provider=provider,
        )
    if provider == "offline_stub":
        return _local_safe_reply(
            question,
            context,
            status_reason="当前 provider=offline_stub。",
            status="local_fallback_stub",
            provider=provider,
        )

    if not is_llm_enabled(config):
        return _safe("LLM 未启用（llm_enabled=false）。在 config/llm_config.json 或环境变量 QUANTDESK_LLM_ENABLED=true 中开启后再对话。", status="disabled", ok=False, provider=provider)
    if not network:
        return _safe("当前为离线模式（network=false），未调用任何 LLM。需显式允许联网后再对话。", status="offline", ok=False, provider=provider)
    if provider == "offline_stub":
        return _safe("当前 provider 为离线模板，不支持多轮对话。请将 default_provider 设为 openai_compatible。", status="offline_stub", ok=False, provider=provider)

    provider_config = get_llm_provider_config(provider, config)
    if not provider_config.get("enabled"):
        return _local_safe_reply(
            question,
            context,
            status_reason=f"{provider} 未启用或未配置。",
            status="local_fallback_provider_disabled",
            provider=provider,
        )
    if not provider_config.get("enabled"):
        return _safe(f"{provider} 未启用或未配置（检查 base_url / model / API Key 环境变量）。", status="provider_disabled", ok=False, provider=provider)

    messages = build_chat_messages(question, context, history, skill_text=load_skill(skill))

    if provider == "openai_compatible":
        from quantdesk.llm_providers import openai_compatible_provider

        result = openai_compatible_provider.chat_completion(messages, provider_config, network=network)
    else:
        return _safe(f"provider={provider} 暂不支持对话（当前仅 openai_compatible）。", status="unsupported_provider", ok=False, provider=provider)

    if not result.get("ok"):
        reason = result.get("failed_reason") or result.get("skipped_reason") or "LLM 调用失败。"
        return _local_safe_reply(
            question,
            context,
            status_reason=str(reason),
            status="local_fallback_llm_failed",
            provider=provider,
        )

    if not result.get("ok"):
        reason = result.get("failed_reason") or result.get("skipped_reason") or "LLM 调用失败。"
        return _safe(f"LLM 调用失败：{reason}", status=str(result.get("status") or "failed"), ok=False, provider=provider)

    raw = str(result.get("report") or "")
    sanitized = sanitize_forbidden_trading_terms(raw)
    residual = detect_forbidden_trading_advice(sanitized)
    grounding = validate_report_grounding(sanitized, build_agent_evidence_pack(context))
    paper_decision = parse_paper_decision_fields(sanitized)

    return {
        "ok": True,
        "status": "ok",
        "reply": sanitized,
        "paper_decision": paper_decision,
        "llm_used": True,
        "provider": provider,
        "policy_guard_status": "passed" if not residual else "sanitized_with_residual",
        "policy_violations": residual,
        "grounding": grounding,
        "skill": skill or "",
        "disclaimer": DISCLAIMER,
    }


__all__ = [
    "observation_chat",
    "build_chat_messages",
    "build_local_observation_reply",
    "parse_paper_decision_fields",
    "list_skills",
    "load_skill",
    "SKILLS_DIR",
    "DISCLAIMER",
]
