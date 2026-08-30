"""Grounding checks for QuantDesk observation reports.

The guard is deterministic and offline.  It checks whether generated report
text stays inside the evidence pack extracted from the local observation
context.
"""

from __future__ import annotations

import re
from typing import Any

from quantdesk.agent.agent_policy_guard import detect_forbidden_trading_advice
from quantdesk.shared import short_error_text

DISCLAIMER_SNIPPETS = (
    "不构成投资建议",
    "仅用于本地学习研究",
    "learning observation",
    "not investment advice",
)
TRADING_CLAIM_TERMS = (
    "收益率",
    "目标价",
    "止损价",
    "仓位",
    "回报率",
    "盈利预测",
    "保证收益",
    "target price",
    "stop loss",
    "position size",
    "guaranteed return",
    "return forecast",
)


def _allowed_set(pack: dict[str, Any], key: str) -> set[str]:
    values = pack.get(key) if isinstance(pack, dict) else []
    if not isinstance(values, list):
        return set()
    return {str(item) for item in values if item is not None and str(item) != ""}


def _line_is_section_number(line: str, token: str) -> bool:
    pattern = rf"^\s*{re.escape(token)}\s*[.、)]\s*\S+"
    return bool(re.search(pattern, line))


def _token_in_version(text: str, token: str) -> bool:
    return bool(re.search(r"\bv?\d+(?:\.\d+){1,3}\b", text, flags=re.I)) and token in text


def _extract_report_numbers(report_text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in str(report_text or "").splitlines():
        clean = line.strip()
        if not clean:
            continue
        if any(snippet in clean for snippet in DISCLAIMER_SNIPPETS):
            continue
        if re.search(r"\bv\d+(?:\.\d+){1,3}\b", clean, flags=re.I):
            # Version labels such as v3.2.1 are metadata, not market facts.
            version_removed = re.sub(r"\bv\d+(?:\.\d+){1,3}\b", " ", clean, flags=re.I)
        else:
            version_removed = clean
        for match in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", version_removed):
            if _line_is_section_number(clean, match):
                continue
            rows.append((match, clean))
    return rows


def detect_unsupported_numbers(report_text: str, evidence_pack: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = _allowed_set(evidence_pack, "allowed_numbers")
    allowed.update(_allowed_set(evidence_pack, "allowed_symbols"))
    allowed.update({str(evidence_pack.get("symbol") or ""), str(evidence_pack.get("date") or "")})
    issues: list[dict[str, Any]] = []
    for token, line in _extract_report_numbers(report_text):
        if token in allowed:
            continue
        if token.zfill(6) in allowed:
            continue
        issues.append({"type": "unsupported_number", "value": token, "excerpt": line[:160]})
    return issues


def detect_unsupported_dates(report_text: str, evidence_pack: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = _allowed_set(evidence_pack, "allowed_dates")
    if evidence_pack.get("date"):
        allowed.add(str(evidence_pack.get("date")))
    issues: list[dict[str, Any]] = []
    for line in str(report_text or "").splitlines():
        for date_text in re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", line):
            if date_text not in allowed:
                issues.append({"type": "unsupported_date", "value": date_text, "excerpt": line.strip()[:160]})
    return issues


def detect_unsupported_symbols(report_text: str, evidence_pack: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = _allowed_set(evidence_pack, "allowed_symbols")
    if evidence_pack.get("symbol"):
        allowed.add(str(evidence_pack.get("symbol")).zfill(6)[-6:])
    issues: list[dict[str, Any]] = []
    for line in str(report_text or "").splitlines():
        for match in re.findall(r"(?<!\d)(?:sh|sz|bj)?([0-9]{6})(?:\.(?:SH|SZ|BJ))?(?!\d)", line, flags=re.I):
            symbol = match.zfill(6)[-6:]
            if symbol not in allowed:
                issues.append({"type": "unsupported_symbol", "value": symbol, "excerpt": line.strip()[:160]})
    return issues


def _pack_has_any(pack: dict[str, Any], *terms: str) -> bool:
    haystack = " ".join(
        [
            " ".join(_allowed_set(pack, "allowed_status_terms")),
            " ".join(str(item) for item in pack.get("data_gaps", []) if item),
            " ".join(str(item) for item in pack.get("warnings", []) if item),
            " ".join(str(item) for item in pack.get("allowed_facts", []) if item),
        ]
    ).lower()
    return any(term.lower() in haystack for term in terms)


def detect_unsupported_provider_claims(report_text: str, evidence_pack: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(report_text or "").lower()
    issues: list[dict[str, Any]] = []
    if _pack_has_any(evidence_pack, "missing_intraday", "cannot_verify_intraday_trigger", "historical_intraday_required"):
        if any(term in text for term in ("分钟线可用", "分钟数据可用", "intraday available", "minute data available")):
            issues.append(
                {
                    "type": "contradiction",
                    "value": "intraday_available_claim",
                    "excerpt": "报告声称分钟线可用，但 evidence pack 标记分钟线缺失或不可验证。",
                }
            )
    if _pack_has_any(evidence_pack, "provider_failed", "provider_skipped", "failed", "skipped", "not_available"):
        success_terms = ("provider 成功", "provider成功", "数据源成功", "provider success", "provider ok")
        if any(term in text for term in success_terms):
            issues.append(
                {
                    "type": "contradiction",
                    "value": "provider_success_claim",
                    "excerpt": "报告声称 provider 成功，但 evidence pack 包含 failed/skipped/not_available。",
                }
            )
    provider_terms = _allowed_set(evidence_pack, "allowed_provider_terms")
    for provider in ("tencent", "mootdx", "akshare", "openai_compatible", "ollama"):
        if provider in text and provider not in " ".join(provider_terms).lower():
            issues.append({"type": "unsupported_provider_claim", "value": provider, "excerpt": provider})
    return issues


# Markers that turn trading vocabulary into a boundary / negation / missing-data
# statement instead of an actual claim. A line containing any of these is treated
# as non-asserting, so a REQUIRED boundary disclaimer or a data-gap
# note like "成交额缺失" is not mistaken for trading advice.
_NON_ASSERTING_MARKERS = (
    "不构成", "不提供", "不预测", "不涉及", "不给出", "不给", "不做", "不是",
    "不能", "无法", "缺失", "缺口", "没有", "未提供", "未给出", "而非", "并非",
    "仅供", "仅用于", "边界",
    "not investment", "not position", "research only", "paper account",
    "no broker", "no real order", "disclaimer",
)


def _line_is_non_asserting(line: str) -> bool:
    low = str(line or "").lower()
    return any(marker in low for marker in _NON_ASSERTING_MARKERS)


def detect_unsupported_trading_claims(report_text: str, evidence_pack: dict[str, Any]) -> list[dict[str, Any]]:
    del evidence_pack
    text = str(report_text or "")
    issues: list[dict[str, Any]] = []
    # Genuine advice-shaped phrases from policy FORBIDDEN_TERMS are always flagged,
    # are always flagged, wherever they appear.
    forbidden = detect_forbidden_trading_advice(text)
    for term in forbidden:
        issues.append({"type": "unsupported_trading_claim", "value": term, "excerpt": term})
    # The broader trading vocabulary (仓位/收益率/回报率...) is only a problem when
    # ASSERTED. The skill REQUIRES boundary, negating, or missing-data lines, so a
    # bare term inside a disclaiming, negating,
    # or missing-data line must NOT be flagged — otherwise the guard punishes the
    # model for correctly keeping its boundaries.
    for line in text.splitlines():
        if _line_is_non_asserting(line):
            continue
        for term in TRADING_CLAIM_TERMS:
            if re.search(re.escape(term), line, flags=re.I):
                issues.append({"type": "unsupported_trading_claim", "value": term, "excerpt": line.strip()[:160]})
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for issue in issues:
        dedup[(str(issue.get("type")), str(issue.get("value")).lower())] = issue
    return list(dedup.values())


def sanitize_or_flag_unsupported_claims(report_text: str, evidence_pack: dict[str, Any]) -> dict[str, Any]:
    validation = validate_report_grounding(report_text, evidence_pack)
    sanitized = str(report_text or "")
    if validation["issues"]:
        banner = "\n\n> Grounding guard: 部分表述无法从本地 observation context 直接追溯，已标记为仅供数据缺口排查。"
        sanitized = sanitized + banner
    validation["sanitized_report"] = sanitized
    return validation


def validate_report_grounding(report_text: str, evidence_pack: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    if not isinstance(evidence_pack, dict) or not evidence_pack:
        warnings.append("empty_evidence_pack")
    issues: list[dict[str, Any]] = []
    issues.extend(detect_unsupported_numbers(report_text, evidence_pack))
    issues.extend(detect_unsupported_dates(report_text, evidence_pack))
    issues.extend(detect_unsupported_symbols(report_text, evidence_pack))
    issues.extend(detect_unsupported_provider_claims(report_text, evidence_pack))
    issues.extend(detect_unsupported_trading_claims(report_text, evidence_pack))
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for issue in issues:
        key = (str(issue.get("type")), str(issue.get("value")).lower())
        dedup[key] = {
            "type": short_error_text(issue.get("type")),
            "value": short_error_text(issue.get("value")),
            "excerpt": short_error_text(issue.get("excerpt")),
        }
    clean_issues = list(dedup.values())
    issue_count = len(clean_issues)
    grounding_score = max(0.0, round(1.0 - min(issue_count, 10) * 0.1, 3))
    return {
        "ok": issue_count == 0,
        "status": "passed" if issue_count == 0 else "guarded",
        "issues": clean_issues,
        "issue_count": issue_count,
        "sanitized_report": str(report_text or ""),
        "grounding_score": grounding_score,
        "warnings": [short_error_text(item) for item in warnings],
    }
