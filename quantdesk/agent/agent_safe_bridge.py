"""Offline-safe bridge for QuantDesk observation reports.

The bridge prepares read-only prompt context, deterministic local fallback
reports, optional LLM calls behind explicit network/config gates, and grounding
metadata.  It is research-only: it cannot modify strategy outputs, provider
data, paper portfolio state, or create trade/order instructions.
"""

from __future__ import annotations

import json
from typing import Any

from quantdesk.agent.agent_evidence_pack import (
    build_agent_evidence_pack,
    build_evidence_summary_for_prompt,
)
from quantdesk.agent.agent_hallucination_guard import validate_report_grounding
from quantdesk.agent.agent_policy_guard import (
    build_policy_violation_result,
    sanitize_forbidden_trading_terms,
    validate_agent_allowed_output,
)
from quantdesk.agent.agent_report_audit import compute_safe_hash
from quantdesk.llm_config import get_llm_provider_config, is_llm_enabled, load_llm_config
from quantdesk.llm_providers.base import build_llm_result
from quantdesk.shared import now_text, short_error_text


def build_agent_system_rules() -> str:
    return (
        "You are a read-only QuantDesk learning observation assistant. "
        "Use only the provided evidence pack and observation context. "
        "When data is missing, say it is missing. Do not add outside market "
        "data, news, forecasts, prices, returns, orders, target levels, risk "
        "threshold levels, or sizing. Do not modify strategy outputs, provider "
        "data, or paper portfolio state. Never hide strategy validation warnings. "
        "The report is only for learning review and is not investment advice."
    )


def build_agent_readonly_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "readonly_learning_observation",
        "context": context,
        "allowed_agent_actions": context.get("allowed_agent_actions") or [],
        "forbidden_agent_actions": context.get("forbidden_agent_actions") or [],
        "network": False,
        "llm_called": False,
        "can_modify_strategy": False,
        "can_modify_portfolio": False,
        "can_call_provider": False,
    }


def build_agent_user_prompt(context: dict[str, Any]) -> str:
    evidence_summary = build_evidence_summary_for_prompt(context)
    compact = {
        "symbol": context.get("symbol"),
        "date": context.get("date"),
        "data_health": context.get("data_health"),
        "strategy_outputs": context.get("strategy_outputs"),
        "intraday_status": context.get("intraday_status"),
        "paper_portfolio_status": context.get("paper_portfolio_status"),
        "provider_chain": context.get("provider_chain"),
        "data_source_trace": context.get("data_source_trace"),
        "validation_summary": context.get("validation_summary"),
        "feature_quality_summary": context.get("feature_quality_summary"),
        "statistical_validation_summary": context.get("statistical_validation_summary"),
        "data_gaps": context.get("data_gaps"),
        "chanlun": context.get("chanlun"),
        "warnings": context.get("warnings"),
        "allowed_agent_actions": context.get("allowed_agent_actions"),
        "forbidden_agent_actions": context.get("forbidden_agent_actions"),
    }
    return (
        "请生成一份**有实际内容**的中文学习复盘报告。"
        "报告必须包含以下章节：数据完整性分析、策略观察状态、缠论结构观察、"
        "分钟线与触发验证、风险与不确定性、下一步观察清单、边界说明。"
        "每个章节必须有具体的分析和解释，不能只列出字段名。\n\n"
        "重要规则：\n"
        "1. 不要原样复制下方 <evidence_pack_reference> 块中的文本到你的报告中。"
        "那个块仅供你内部参考，用于理解数据上下文。"
        "2. 用你自己的语言撰写报告，解释数据说明了什么、有什么风险、下一步该观察什么。\n"
        "3. 如果某个字段值为空或缺失，请在对应章节中说明'该数据暂未提供'，"
        "但不要输出字段名本身作为报告内容。\n"
        "4. 不要输出交易指令、价格结论或资金安排。\n\n"
        "<evidence_pack_reference>\n"
        f"{evidence_summary}\n"
        "</evidence_pack_reference>\n\n"
        "Observation context:\n"
        + json.dumps(compact, ensure_ascii=False, indent=2, default=str)
    )


def sanitize_agent_output(text: Any) -> str:
    return sanitize_forbidden_trading_terms(text)


def validate_agent_output(text: Any) -> dict[str, Any]:
    return validate_agent_allowed_output(text)


def select_llm_provider(llm_config: dict[str, Any] | None = None) -> str:
    config = llm_config or load_llm_config()
    provider = str(config.get("default_provider") or "offline_stub").strip() or "offline_stub"
    providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
    if provider not in providers:
        return "offline_stub"
    return provider


def build_llm_prompt_bundle(context: dict[str, Any]) -> dict[str, str]:
    system_rules = build_agent_system_rules()
    user_prompt = build_agent_user_prompt(context)
    return {
        "system_rules": system_rules,
        "user_prompt": user_prompt,
        "prompt_hash": compute_safe_hash({"system_rules": system_rules, "user_prompt": user_prompt}),
    }


def _context_list(context: dict[str, Any], key: str) -> list[Any]:
    value = context.get(key) if isinstance(context, dict) else []
    return value if isinstance(value, list) else ([] if not value else [value])


def _brief_dict(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "缺失"
    fields = []
    labels = {
        "status": "状态",
        "summary_text": "摘要",
        "normalized_trigger_status": "触发验证",
        "explanation": "说明",
    }
    for key in ("status", "summary_text", "normalized_trigger_status", "explanation"):
        if value.get(key):
            fields.append(f"{labels[key]}：{value.get(key)}")
    return "；".join(fields) if fields else "已记录，但缺少摘要字段"


def generate_stub_observation_report(context: dict[str, Any]) -> dict[str, Any]:
    """Generate a deterministic local report without invoking any LLM."""

    symbol = str(context.get("symbol") or "未指定")
    date_text = str(context.get("date") or "未指定")
    data_gaps = [str(item) for item in _context_list(context, "data_gaps") if item]
    warnings = [str(item) for item in _context_list(context, "warnings") if item]
    provider_chain = [str(item) for item in _context_list(context, "provider_chain") if item]
    data_health = context.get("data_health") if isinstance(context.get("data_health"), dict) else {}
    data_health_summary = context.get("data_health_summary") if isinstance(context.get("data_health_summary"), dict) else {}
    stale_data_warning = context.get("stale_data_warning") if isinstance(context.get("stale_data_warning"), dict) else {}
    strategy_outputs = context.get("strategy_outputs") if isinstance(context.get("strategy_outputs"), dict) else {}
    intraday = context.get("intraday_status") if isinstance(context.get("intraday_status"), dict) else {}
    chanlun = context.get("chanlun") if isinstance(context.get("chanlun"), dict) else {}
    validation_summary = context.get("validation_summary") if isinstance(context.get("validation_summary"), dict) else {}
    feature_quality_summary = context.get("feature_quality_summary") if isinstance(context.get("feature_quality_summary"), dict) else {}
    statistical_validation_summary = context.get("statistical_validation_summary") if isinstance(context.get("statistical_validation_summary"), dict) else {}
    minute_pool = intraday.get("verified_minute_pool") if isinstance(intraday.get("verified_minute_pool"), dict) else {}
    verified_minute_count = minute_pool.get("available_count") or intraday.get("verified_completed_intraday_count")
    high_missing_display = (
        feature_quality_summary.get("high_missing_feature_count")
        if feature_quality_summary.get("exists")
        else "missing"
    )

    if context.get("cannot_verify_intraday_trigger"):
        intraday_line = intraday.get("explanation") or "分钟线数据缺失或不可验证，只能生成数据缺口说明。"
    else:
        intraday_line = intraday.get("explanation") or "分钟线状态未提供明确摘要。"
    gap_line = "；".join(data_gaps) if data_gaps else "未记录明确数据缺口。"
    if validation_summary.get("status") == "failed":
        warnings = ["当前策略验证存在风险，学习复盘报告不应被解读为可靠策略结论。", *warnings]
    if int(feature_quality_summary.get("high_missing_feature_count") or 0) > 0:
        warnings = ["部分特征字段缺失率较高，策略观察结论可信度下降。", *warnings]
    for note in statistical_validation_summary.get("risk_notes") or []:
        warnings = [str(note), *warnings]
    if stale_data_warning.get("stale"):
        warnings = ["本地日线数据未更新到目标完整交易日，学习复盘可信度下降。", *warnings]
    warning_line = "；".join(warnings[:8]) if warnings else "未记录额外警告。"
    provider_line = " -> ".join(provider_chain) if provider_chain else "provider 链路未提供。"
    chanlun_line = "；".join(
        f"{key}={_brief_dict(value)}" for key, value in chanlun.items() if isinstance(value, dict)
    ) or "缠论结构未提供可追溯摘要。"
    data_health_line = _brief_dict(data_health)
    data_health_summary_line = _brief_dict(data_health_summary)
    strategy_line = _brief_dict(strategy_outputs)
    feature_line = (
        f"状态 {feature_quality_summary.get('status') or 'missing'}，高缺失字段数 {high_missing_display}。"
    )
    statistical_line = (
        f"状态 {statistical_validation_summary.get('status') or 'missing'}，"
        f"p 值 {statistical_validation_summary.get('p_value') if statistical_validation_summary.get('p_value') is not None else '未提供'}。"
    )
    minute_pool_line = (
        f"已验证分钟池覆盖 {verified_minute_count} 个标的；"
        if verified_minute_count is not None
        else ""
    )

    report = f"""# QuantDesk 学习复盘报告

本报告仅基于本地 strategy observation context 和 evidence pack 生成，用于学习复盘，不构成投资建议。

一、数据完整性
股票代码：{symbol}
观察日期：{date_text}
数据健康：{data_health_line}
数据健康摘要：{data_health_summary_line}
数据源链路：{provider_line}
数据缺口：{gap_line}
特征审计：{feature_line}
统计校验：{statistical_line}

二、策略观察状态
策略观察：{strategy_line}
观察边界：{"仅观察模式" if context.get("observation_only") else "可做本地纸面复盘观察"}

三、缠论结构观察
{chanlun_line}

四、分钟线与触发验证
{minute_pool_line}{intraday_line}

五、风险与不确定性
{warning_line}

六、下一步观察清单
- 核对本地 observation context 是否包含完整数据健康摘要。
- 如分钟线缺失，优先补充本地分钟线 CSV 后再复核触发验证。
- 复查 provider 链路状态，区分 skipped、failed 与 ok。

七、边界说明
当前报告为只读学习复盘，不接券商、不真实下单、不修改策略结果、不修改纸面组合。"""
    sanitized = sanitize_agent_output(report)
    validation = validate_agent_output(sanitized)
    if not validation.get("ok"):
        violation = build_policy_violation_result(report)
        sanitized = str(violation.get("sanitized_text") or sanitized)
        validation = validate_agent_output(sanitized)
    return {
        "ok": bool(validation.get("ok")),
        "report": sanitized,
        "validation": validation,
        "llm_called": False,
        "network": False,
        "disclaimer": "仅用于本地学习研究，不构成投资建议。",
    }


def _apply_grounding_metadata(
    payload: dict[str, Any],
    context: dict[str, Any],
    prompt_bundle: dict[str, Any],
    *,
    strict_grounding: bool = True,
) -> dict[str, Any]:
    evidence_pack = build_agent_evidence_pack(context)
    report = str(payload.get("report") or "")
    grounding = validate_report_grounding(report, evidence_pack)
    sanitized_report = str(grounding.get("sanitized_report") or report)
    payload["report"] = sanitized_report
    payload["evidence_pack_used"] = True
    payload["evidence_pack_hash"] = compute_safe_hash(evidence_pack)
    payload["grounding_guard_status"] = grounding.get("status")
    payload["grounding_score"] = grounding.get("grounding_score")
    payload["unsupported_issue_count"] = grounding.get("issue_count")
    payload["unsupported_issues"] = grounding.get("issues") or []
    payload["sanitized_by_grounding_guard"] = sanitized_report != report
    payload["prompt_hash"] = prompt_bundle.get("prompt_hash") or compute_safe_hash(prompt_bundle)
    payload["context_hash"] = compute_safe_hash(context)
    payload["report_hash"] = compute_safe_hash(sanitized_report)
    payload.setdefault("warnings", [])
    if grounding.get("warnings"):
        payload["warnings"] = [*payload.get("warnings", []), *grounding.get("warnings", [])]
    if grounding.get("issue_count"):
        payload["warnings"] = [
            *payload.get("warnings", []),
            f"grounding_guard_issues={grounding.get('issue_count')}",
        ]
        if strict_grounding:
            payload["ok"] = False
            payload["status"] = "guarded"
        else:
            payload.setdefault("status", "warning")
    return payload


def validate_llm_report_result(
    result: dict[str, Any],
    context: dict[str, Any] | None = None,
    prompt_bundle: dict[str, Any] | None = None,
    *,
    strict_grounding: bool = True,
) -> dict[str, Any]:
    payload = dict(result or {})
    report = str(payload.get("report") or "")
    sanitized = sanitize_agent_output(report)
    validation = validate_agent_output(sanitized)
    if validation.get("ok"):
        status = "passed" if sanitized == report else "sanitized"
    else:
        violation = build_policy_violation_result(sanitized)
        sanitized = str(violation.get("sanitized_text") or sanitized)
        validation = validate_agent_output(sanitized)
        status = "passed" if validation.get("ok") else "blocked"
        payload["policy_violation"] = violation
    payload["report"] = sanitized
    payload["policy_guard_status"] = status
    payload["policy_guard"] = validation
    payload["ok"] = bool(payload.get("ok", True)) and status != "blocked"
    if status == "blocked":
        payload["status"] = "policy_guarded"
    if context is not None:
        payload = _apply_grounding_metadata(
            payload,
            context,
            prompt_bundle or build_llm_prompt_bundle(context),
            strict_grounding=strict_grounding,
        )
    return payload


def fallback_to_stub_report(
    context: dict[str, Any],
    reason: str,
    *,
    provider: str = "offline_stub",
    strict_grounding: bool = True,
) -> dict[str, Any]:
    prompt_bundle = build_llm_prompt_bundle(context)
    stub = generate_stub_observation_report(context)
    validation = stub.get("validation") if isinstance(stub.get("validation"), dict) else {}
    policy_status = "passed" if validation.get("ok", True) else "blocked"
    payload = build_llm_result(
        ok=bool(stub.get("ok")),
        status="fallback",
        provider="offline_stub",
        llm_used=False,
        fallback_used=True,
        skipped_reason=short_error_text(reason),
        warnings=[f"requested_provider={provider}"],
        policy_guard_status=policy_status,
        report=str(stub.get("report") or ""),
        generated_at=now_text(),
    )
    return validate_llm_report_result(
        payload,
        context,
        prompt_bundle,
        strict_grounding=strict_grounding,
    )


def _call_llm_provider(
    provider: str,
    prompt_bundle: dict[str, str],
    context: dict[str, Any],
    provider_config: dict[str, Any],
    *,
    network: bool,
) -> dict[str, Any]:
    if provider == "openai_compatible":
        from quantdesk.llm_providers import openai_compatible_provider

        return openai_compatible_provider.generate_observation_report(prompt_bundle, context, provider_config, network=network)
    if provider == "ollama":
        from quantdesk.llm_providers import ollama_provider

        return ollama_provider.generate_observation_report(prompt_bundle, context, provider_config, network=network)
    from quantdesk.llm_providers import offline_stub_provider

    return offline_stub_provider.generate_observation_report(prompt_bundle, context, provider_config)


def generate_llm_observation_report(
    context: dict[str, Any],
    llm_config: dict[str, Any] | None = None,
    *,
    network: bool = False,
    strict_grounding: bool = False,
) -> dict[str, Any]:
    """Generate a learning review with optional LLM, evidence guard, and fallback."""

    config = llm_config or load_llm_config()
    provider = select_llm_provider(config)
    if not is_llm_enabled(config):
        return fallback_to_stub_report(
            context,
            "llm_enabled=false; 使用离线模板报告",
            provider=provider,
            strict_grounding=strict_grounding,
        )
    if not network:
        return fallback_to_stub_report(
            context,
            "network=false; 不调用任何 LLM",
            provider=provider,
            strict_grounding=strict_grounding,
        )

    provider_config = get_llm_provider_config(provider, config)
    if provider == "offline_stub":
        return fallback_to_stub_report(
            context,
            "provider=offline_stub; 使用离线模板报告",
            provider=provider,
            strict_grounding=strict_grounding,
        )
    if not provider_config.get("enabled"):
        return fallback_to_stub_report(
            context,
            f"{provider} disabled or missing config; 使用离线模板报告",
            provider=provider,
            strict_grounding=strict_grounding,
        )

    prompt_bundle = build_llm_prompt_bundle(context)
    result = _call_llm_provider(provider, prompt_bundle, context, provider_config, network=network)
    if not result.get("ok"):
        reason = result.get("failed_reason") or result.get("skipped_reason") or "LLM provider failed"
        fallback = fallback_to_stub_report(
            context,
            reason,
            provider=provider,
            strict_grounding=strict_grounding,
        )
        fallback["provider_result"] = {
            "provider": result.get("provider"),
            "status": result.get("status"),
            "failed_reason": result.get("failed_reason"),
            "skipped_reason": result.get("skipped_reason"),
            "warnings": result.get("warnings") or [],
        }
        return fallback

    result.setdefault("provider", provider)
    result.setdefault("llm_used", provider != "offline_stub")
    result.setdefault("fallback_used", False)
    result.setdefault("generated_at", now_text())
    result.setdefault("disclaimer", "仅用于本地学习研究，不构成投资建议。")
    return validate_llm_report_result(
        result,
        context,
        prompt_bundle,
        strict_grounding=strict_grounding,
    )


__all__ = [
    "build_agent_system_rules",
    "build_agent_readonly_context",
    "build_agent_user_prompt",
    "sanitize_agent_output",
    "validate_agent_output",
    "select_llm_provider",
    "build_llm_prompt_bundle",
    "generate_stub_observation_report",
    "validate_llm_report_result",
    "fallback_to_stub_report",
    "generate_llm_observation_report",
]
