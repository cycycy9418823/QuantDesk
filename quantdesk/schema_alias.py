"""Schema alias helpers for strategy-facing rows.

The project has moved from early demo names such as ``activity_avg_20`` and
``data_reliability_score`` to the v2.7 data-confidence vocabulary.  This module
keeps older result files readable without making UI pages show stale missing
field warnings.
"""

from __future__ import annotations

from typing import Any

FIELD_ALIASES: dict[str, str] = {
    "activity_avg_20": "amount_ma20",
    "activity_status": "liquidity_status",
    "data_reliability_score": "data_confidence_score",
    "data_reliability_status": "data_confidence_status",
    "data_reliability_note": "data_confidence_note",
}

FIELD_LABELS: dict[str, str] = {
    "amount_ma20": "20日均成交额",
    "volume_ma20": "20日均成交量",
    "liquidity_status": "成交活跃度状态",
    "data_confidence_score": "数据可信度评分",
    "data_confidence_status": "数据可信度状态",
    "data_confidence_note": "数据可信度说明",
}

FIELD_IMPACTS: dict[str, str] = {
    "amount_ma20": "无法判断成交活跃度",
    "volume_ma20": "无法用成交量补充判断活跃度",
    "liquidity_status": "无法判断是否适合进入本地虚拟组合",
    "data_confidence_score": "无法量化数据可信度",
    "data_confidence_status": "风控无法判断数据是否可靠",
    "data_confidence_note": "缺少数据可信度解释",
}

FIELD_SOLUTIONS: dict[str, str] = {
    "amount_ma20": "检查日线 CSV 是否有 amount / turnover / 成交额 字段，并重新生成低价学习池报告",
    "volume_ma20": "检查日线 CSV 是否有 volume / 成交量 字段，并重新生成低价学习池报告",
    "liquidity_status": "重新生成低价学习池报告，让系统基于 amount_ma20 / volume_ma20 计算状态",
    "data_confidence_score": "重新生成低价报告或检查日线字段完整性",
    "data_confidence_status": "重新生成低价报告或检查日线字段完整性",
    "data_confidence_note": "重新生成低价报告或检查日线字段完整性",
}


def canonical_field_name(field: str) -> str:
    """Return the current schema name for a field."""

    return FIELD_ALIASES.get(str(field or "").strip(), str(field or "").strip())


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def normalize_strategy_item_fields(item: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy with old strategy field names mapped to current names.

    The original keys are kept for backwards compatibility, but missing-field
    lists are canonicalized so UI pages do not report ``activity_avg_20`` as
    missing when ``amount_ma20`` is present.
    """

    normalized = dict(item or {})
    for old_name, new_name in FIELD_ALIASES.items():
        if not _has_value(normalized.get(new_name)) and _has_value(normalized.get(old_name)):
            normalized[new_name] = normalized.get(old_name)

    for list_key in ("missing_required_fields", "missing_optional_fields", "missing_fields"):
        fields = normalized.get(list_key)
        if not isinstance(fields, list):
            continue
        cleaned: list[str] = []
        for raw in fields:
            canonical = canonical_field_name(str(raw))
            if _has_value(normalized.get(canonical)):
                continue
            if canonical not in cleaned:
                cleaned.append(canonical)
        normalized[list_key] = cleaned
    return normalized


def missing_field_diagnostics(fields: list[str], item: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Build user-facing diagnostics for missing fields."""

    normalized = normalize_strategy_item_fields(item or {})
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in fields:
        field = canonical_field_name(str(raw))
        if field in seen or _has_value(normalized.get(field)):
            continue
        seen.add(field)
        rows.append(
            {
                "field": field,
                "label": FIELD_LABELS.get(field, "未知字段"),
                "impact": FIELD_IMPACTS.get(field, "该字段缺失会降低规则解释完整度"),
                "next_step": FIELD_SOLUTIONS.get(field, "重新生成相关本地报告或检查 CSV 字段"),
            }
        )
    return rows


def required_field_status(item: dict[str, Any] | None, required_fields: list[str]) -> dict[str, Any]:
    """Summarize required-field availability using the current schema names."""

    normalized = normalize_strategy_item_fields(item or {})
    missing = [canonical_field_name(field) for field in required_fields if not _has_value(normalized.get(canonical_field_name(field)))]
    missing = list(dict.fromkeys(missing))
    return {
        "ok": not missing,
        "missing_fields": missing,
        "diagnostics": missing_field_diagnostics(missing, normalized),
    }
