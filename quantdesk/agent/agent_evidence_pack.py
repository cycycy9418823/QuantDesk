"""Evidence pack helpers for read-only observation agents.

The evidence pack is a compact, sanitized view of a strategy observation
context.  It gives an optional LLM a bounded set of facts and also feeds the
grounding guard used after report generation.
"""

from __future__ import annotations

import re
from typing import Any

from quantdesk.shared import now_text, short_error_text

SCHEMA_VERSION = "3.2.1"
DISCLAIMER = "仅用于本地学习研究，不构成投资建议。"
SECRET_KEYWORDS = ("api_key", "apikey", "token", "secret", "password", "credential")
STATUS_TERMS = {
    "ok",
    "failed",
    "skipped",
    "partial",
    "no_data",
    "not_available",
    "missing_intraday",
    "no_fill",
    "provider_failed",
    "provider_skipped",
    "current_day_only",
    "cannot_verify_intraday_trigger",
    "observation_only",
    "data_unreliable",
    "historical_intraday_required",
}
PROVIDER_HINTS = ("tencent", "mootdx", "akshare", "local", "offline_stub", "openai", "ollama", "provider")


def _is_secret_key(key: Any) -> bool:
    text = str(key or "").lower()
    return any(marker in text for marker in SECRET_KEYWORDS)


def _safe_scalar(value: Any) -> str:
    text = str(value)
    if any(marker in text.lower() for marker in SECRET_KEYWORDS):
        return "[redacted]"
    return text.strip()


def _walk_scalars(value: Any, *, path: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_secret_key(key):
                continue
            next_path = f"{path}.{key}" if path else str(key)
            rows.extend(_walk_scalars(item, path=next_path))
        return rows
    if isinstance(value, list):
        for index, item in enumerate(value):
            rows.extend(_walk_scalars(item, path=f"{path}[{index}]"))
        return rows
    if value is None:
        return rows
    rows.append((path, value))
    return rows


def flatten_context_facts(context: dict[str, Any]) -> list[str]:
    """Flatten scalar context values into short, sanitized fact strings."""

    facts: list[str] = []
    for path, value in _walk_scalars(context if isinstance(context, dict) else {}):
        text = _safe_scalar(value)
        if not text or text == "[redacted]":
            continue
        facts.append(f"{path}={text}"[:500])
    return sorted(dict.fromkeys(facts))


def extract_allowed_numbers(context: dict[str, Any]) -> list[str]:
    numbers: set[str] = set()
    for _path, value in _walk_scalars(context if isinstance(context, dict) else {}):
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            numbers.add(str(value))
            continue
        if isinstance(value, float):
            numbers.add(str(value).rstrip("0").rstrip("."))
            continue
        text = _safe_scalar(value)
        for match in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", text):
            numbers.add(match)
    return sorted(numbers, key=lambda item: (len(item), item))


def extract_allowed_dates(context: dict[str, Any]) -> list[str]:
    dates: set[str] = set()
    for _path, value in _walk_scalars(context if isinstance(context, dict) else {}):
        text = _safe_scalar(value)
        for match in re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", text):
            dates.add(match)
    for key in ("date", "target_date", "generated_at", "retrieved_at"):
        value = context.get(key) if isinstance(context, dict) else ""
        if value:
            found = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", str(value))
            dates.update(found)
    return sorted(dates)


def extract_allowed_symbols(context: dict[str, Any]) -> list[str]:
    symbols: set[str] = set()
    if isinstance(context, dict) and context.get("symbol"):
        symbols.add(str(context.get("symbol")).zfill(6)[-6:])
    for _path, value in _walk_scalars(context if isinstance(context, dict) else {}):
        text = _safe_scalar(value)
        for match in re.findall(r"(?<!\d)(?:sh|sz|bj)?([0-9]{6})(?:\.(?:SH|SZ|BJ))?(?!\d)", text, flags=re.I):
            symbols.add(match.zfill(6)[-6:])
    return sorted(symbols)


def extract_allowed_status_terms(context: dict[str, Any]) -> list[str]:
    terms: set[str] = set()
    for path, value in _walk_scalars(context if isinstance(context, dict) else {}):
        path_text = path.lower()
        text = _safe_scalar(value).lower()
        for term in STATUS_TERMS:
            if term in path_text or term in text:
                terms.add(term)
    for key in ("observation_only", "cannot_verify_intraday_trigger"):
        if isinstance(context, dict) and context.get(key):
            terms.add(key)
    return sorted(terms)


def extract_allowed_provider_terms(context: dict[str, Any]) -> list[str]:
    terms: set[str] = set()
    provider_chain = context.get("provider_chain") if isinstance(context, dict) else None
    if isinstance(provider_chain, list):
        terms.update(str(item) for item in provider_chain if item)
    trace = context.get("data_source_trace") if isinstance(context, dict) else None
    if isinstance(trace, list):
        for item in trace:
            if isinstance(item, dict):
                for key in ("provider", "provider_used", "source"):
                    if item.get(key):
                        terms.add(str(item.get(key)))
            elif item:
                terms.add(str(item))
    # Walk scalars: only add the value when the hint appears IN the value text
    # itself (meaning the value is an actual provider name like "tencent_daily").
    # Do NOT add values just because the path contains a hint (e.g.
    # "local_daily_latest_date" should not leak its date value as a provider term).
    for path, value in _walk_scalars(context if isinstance(context, dict) else {}):
        value_text = _safe_scalar(value).lower()
        for hint in PROVIDER_HINTS:
            if hint in value_text:
                terms.add(_safe_scalar(value))
            elif hint == "provider" and hint in path.lower():
                terms.add(_safe_scalar(value))
    clean = {term.strip() for term in terms if term and not _is_secret_key(term)}
    return sorted(clean)


def _extract_data_gaps(context: dict[str, Any]) -> list[str]:
    gaps = context.get("data_gaps") if isinstance(context, dict) else []
    if not isinstance(gaps, list):
        return [short_error_text(gaps)] if gaps else []
    return [short_error_text(item) for item in gaps if item]


def _verified_minute_pool_summary(context: dict[str, Any]) -> dict[str, Any]:
    intraday = context.get("intraday_status") if isinstance(context, dict) and isinstance(context.get("intraday_status"), dict) else {}
    pool = intraday.get("verified_minute_pool") if isinstance(intraday.get("verified_minute_pool"), dict) else {}
    if not pool:
        return {}
    warnings = pool.get("warnings") if isinstance(pool.get("warnings"), list) else []
    return {
        "passed": bool(pool.get("passed") or pool.get("status") == "passed"),
        "status": short_error_text(pool.get("status") or "unknown"),
        "available_count": int(pool.get("available_count") or 0),
        "missing_count": int(pool.get("missing_count") or 0),
        "source_snapshot": short_error_text(pool.get("source_snapshot") or ""),
        "latest_quality_status": short_error_text(pool.get("latest_quality_status") or ""),
        "warnings": [short_error_text(item) for item in warnings if item],
    }


def build_agent_evidence_pack(context: dict[str, Any]) -> dict[str, Any]:
    ctx = context if isinstance(context, dict) else {}
    warnings = []
    if not ctx:
        warnings.append("empty_observation_context")
    facts = flatten_context_facts(ctx)
    data_gaps = _extract_data_gaps(ctx)
    minute_pool_summary = _verified_minute_pool_summary(ctx)
    pack = {
        "schema_version": SCHEMA_VERSION,
        "symbol": str(ctx.get("symbol") or ""),
        "date": str(ctx.get("date") or ctx.get("target_date") or ""),
        "generated_at": now_text(),
        "allowed_facts": facts,
        "allowed_numbers": extract_allowed_numbers(ctx),
        "allowed_dates": extract_allowed_dates(ctx),
        "allowed_symbols": extract_allowed_symbols(ctx),
        "allowed_status_terms": extract_allowed_status_terms(ctx),
        "allowed_provider_terms": extract_allowed_provider_terms(ctx),
        "validation_summary": ctx.get("validation_summary") if isinstance(ctx.get("validation_summary"), dict) else {},
        "data_health_summary": ctx.get("data_health_summary") if isinstance(ctx.get("data_health_summary"), dict) else {},
        "feature_quality_summary": ctx.get("feature_quality_summary") if isinstance(ctx.get("feature_quality_summary"), dict) else {},
        "statistical_validation_summary": ctx.get("statistical_validation_summary") if isinstance(ctx.get("statistical_validation_summary"), dict) else {},
        "parameter_sensitivity_summary": ctx.get("parameter_sensitivity_summary") if isinstance(ctx.get("parameter_sensitivity_summary"), dict) else {},
        "verified_minute_pool_summary": minute_pool_summary,
        "data_gaps": data_gaps,
        "warnings": [short_error_text(item) for item in [*warnings, *(ctx.get("warnings") or [])] if item],
        "forbidden_inference_rules": [
            "不能补充 context 中没有的价格",
            "不能补充 context 中没有的涨跌幅",
            "不能补充 context 中没有的成交量",
            "不能补充 context 中没有的收益率",
            "不能补充 context 中没有的买卖结论",
        ],
        "disclaimer": DISCLAIMER,
    }
    validation = validate_evidence_pack(pack)
    pack["ok"] = bool(validation.get("ok"))
    pack["validation"] = validation
    return pack


def build_evidence_summary_for_prompt(context: dict[str, Any]) -> str:
    pack = build_agent_evidence_pack(context)
    symbol_display = pack.get("symbol") or "未指定（全市场观察模式）"
    date_display = pack.get("date") or "未指定"
    parts = [
        f"symbol: {symbol_display}",
        f"date: {date_display}",
        "allowed_status_terms: " + (", ".join(pack.get("allowed_status_terms") or []) or "none"),
        "allowed_provider_terms: " + (", ".join(pack.get("allowed_provider_terms") or []) or "none"),
        "data_gaps: " + (", ".join(pack.get("data_gaps") or []) or "none"),
        "validation_summary: " + short_error_text(pack.get("validation_summary") or {}),
        "data_health_summary: " + short_error_text(pack.get("data_health_summary") or {}),
        "feature_quality_summary: " + short_error_text(pack.get("feature_quality_summary") or {}),
        "statistical_validation_summary: " + short_error_text(pack.get("statistical_validation_summary") or {}),
        "parameter_sensitivity_summary: " + short_error_text(pack.get("parameter_sensitivity_summary") or {}),
        "verified_minute_pool: "
        + (
            f"passed with {pack.get('verified_minute_pool_summary', {}).get('available_count')} covered symbols"
            if pack.get("verified_minute_pool_summary")
            else "none"
        ),
        "warnings: " + (", ".join(pack.get("warnings") or []) or "none"),
        "rules: " + "; ".join(pack.get("forbidden_inference_rules") or []),
    ]
    return "\n".join(parts)


def validate_evidence_pack(pack: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    if not isinstance(pack, dict):
        return {"ok": False, "status": "invalid", "warnings": [], "errors": ["pack_not_dict"]}
    if pack.get("schema_version") != SCHEMA_VERSION:
        warnings.append("schema_version_not_3_2_1")
    for key in ("allowed_facts", "allowed_numbers", "allowed_dates", "allowed_symbols"):
        if not isinstance(pack.get(key), list):
            errors.append(f"{key}_not_list")
    joined = repr(pack).lower()
    if any(marker in joined for marker in SECRET_KEYWORDS):
        errors.append("possible_secret_marker_present")
    return {
        "ok": not errors,
        "status": "ok" if not errors else "failed",
        "warnings": [short_error_text(item) for item in warnings],
        "errors": [short_error_text(item) for item in errors],
    }
