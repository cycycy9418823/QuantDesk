"""Policy guard for agent-facing observation text.

Deterministic and offline: this module does not call an LLM and does not decide
anything about trading. It only keeps QuantDesk's learning reports from drifting
into advice-shaped language by detecting and rewriting forbidden phrases.

The term/replacement tables and helper functions are defined ONCE here. (Earlier
revisions defined the forbidden-term list three times and the helpers twice;
that redundancy has been collapsed into the single canonical definitions below.)
"""

from __future__ import annotations

import re
from typing import Any

# Single source of truth: advice-shaped phrases (Chinese + English) and the
# observation-only wording each is rewritten to.
FORBIDDEN_TERMS: tuple[str, ...] = (
    "买入建议",
    "卖出建议",
    "推荐买入",
    "推荐卖出",
    "建议买入",
    "建议卖出",
    "建议建仓",
    "建议加仓",
    "建议减仓",
    "满仓",
    "梭哈",
    "目标价",
    "止损价",
    "必涨",
    "稳赚",
    "保证收益",
    "buy recommendation",
    "sell recommendation",
    "target price",
    "stop loss",
    "position size",
    "guaranteed return",
    "strong buy",
    "strong sell",
)

REPLACEMENTS: dict[str, str] = {
    "买入建议": "观察信号说明",
    "卖出建议": "风险观察说明",
    "推荐买入": "进入观察清单",
    "推荐卖出": "风险观察清单",
    "建议买入": "进入观察清单",
    "建议卖出": "风险观察清单",
    "建议建仓": "纸面观察记录",
    "建议加仓": "强弱变化观察",
    "建议减仓": "风险变化观察",
    "满仓": "高暴露纸面状态",
    "梭哈": "高风险表达",
    "目标价": "后续观察价位区间说明",
    "止损价": "风险阈值观察",
    "必涨": "不确定走势",
    "稳赚": "不确定结果",
    "保证收益": "不确定收益",
    "buy recommendation": "observation note",
    "sell recommendation": "risk observation note",
    "target price": "observation price area",
    "stop loss": "risk threshold observation",
    "position size": "paper simulation state",
    "guaranteed return": "uncertain return",
    "strong buy": "observation candidate",
    "strong sell": "risk observation",
}


def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(re.escape(term), re.IGNORECASE)


def detect_forbidden_trading_advice(text: Any) -> list[str]:
    """Return forbidden advice-shaped terms found in text."""
    content = str(text or "")
    return [term for term in FORBIDDEN_TERMS if _term_pattern(term).search(content)]


def sanitize_forbidden_trading_terms(text: Any) -> str:
    """Replace forbidden trading-advice phrases with observation wording."""
    content = str(text or "")
    for term, replacement in REPLACEMENTS.items():
        content = _term_pattern(term).sub(replacement, content)
    return content


def validate_agent_allowed_output(text: Any) -> dict[str, Any]:
    sanitized = sanitize_forbidden_trading_terms(text)
    violations = detect_forbidden_trading_advice(sanitized)
    return {
        "ok": not violations,
        "policy": "agent_observation_only",
        "violations": violations,
        "sanitized_text": sanitized,
    }


def build_policy_violation_result(text: Any) -> dict[str, Any]:
    original_violations = detect_forbidden_trading_advice(text)
    sanitized = sanitize_forbidden_trading_terms(text)
    remaining = detect_forbidden_trading_advice(sanitized)
    return {
        "ok": not remaining,
        "policy_violation": bool(original_violations),
        "violations": original_violations,
        "remaining_violations": remaining,
        "sanitized_text": sanitized,
        "message": "Agent output must stay in learning observation mode and cannot contain trading advice.",
    }


__all__ = [
    "FORBIDDEN_TERMS",
    "REPLACEMENTS",
    "detect_forbidden_trading_advice",
    "sanitize_forbidden_trading_terms",
    "validate_agent_allowed_output",
    "build_policy_violation_result",
]
