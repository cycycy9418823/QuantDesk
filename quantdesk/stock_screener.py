"""Configurable stock screener — filter candidates by indicator + factor conditions.

Supports conditions like:
  volume_ratio > 2
  macd_cross == golden
  pe_ttm < 30
  data_grade >= RESEARCH_READY

All results are for local research only. Not investment advice.
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

SCREENER_DISCLAIMER = (
    "选股筛选器仅用于本地条件过滤，不构成入池指令或投资建议，"
    "不保证筛选结果的未来表现，不自动下单。"
)

DATA_GRADE_ORDER = {
    "UNAVAILABLE": 0,
    "OBSERVE_ONLY": 1,
    "SCORABLE": 2,
    "RESEARCH_READY": 3,
    "BACKTEST_READY": 4,
}


@dataclass
class ScreenCondition:
    """One filter condition."""

    field_name: str = ""
    op: str = ">"  # >, <, >=, <=, ==, !=
    value: Any = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "op": self.op,
            "value": self.value,
            "description": self.description,
        }


@dataclass
class ScreenResult:
    """Result from the screener."""

    matched: list[dict[str, Any]] = field(default_factory=list)
    total_scanned: int = 0
    total_matched: int = 0
    conditions: list[ScreenCondition] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    disclaimer: str = SCREENER_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "total_scanned": self.total_scanned,
            "total_matched": self.total_matched,
            "conditions": [c.to_dict() for c in self.conditions],
            "warnings": self.warnings,
            "disclaimer": self.disclaimer,
        }


_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


def _compare_data_grade(actual: str, op: str, target: str) -> bool:
    """Compare data grades using ordinal order."""
    a = DATA_GRADE_ORDER.get(str(actual).upper(), -1)
    b = DATA_GRADE_ORDER.get(str(target).upper(), -1)
    fn = _OPS.get(op)
    if fn is None:
        return False
    return fn(a, b)


def _get_nested_value(data: Mapping[str, Any], key: str) -> Any:
    """Get value from nested dict using dot notation."""
    parts = key.split(".")
    current: Any = data
    for part in parts:
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            return None
    return current


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _check_condition(row: Mapping[str, Any], cond: ScreenCondition) -> bool:
    """Check if a row satisfies a condition."""
    value = _get_nested_value(row, cond.field_name)
    if value is None:
        return False

    target = cond.value
    op = cond.op

    # special handling for data_grade
    if cond.field_name == "data_grade":
        return _compare_data_grade(str(value), op, str(target))

    # boolean comparison
    if isinstance(target, bool):
        parsed = _coerce_bool(value)
        if parsed is None:
            return False
        return _OPS.get(op, operator.eq)(parsed, target)

    # string comparison
    if isinstance(target, str):
        return _OPS.get(op, operator.eq)(str(value), target)

    # numeric comparison
    try:
        num_value = float(value)
        num_target = float(target)
        fn = _OPS.get(op)
        return fn(num_value, num_target) if fn else False
    except (ValueError, TypeError):
        return False


def parse_condition(text: str) -> ScreenCondition | None:
    """Parse a condition string like 'volume_ratio > 2' or 'data_grade >= RESEARCH_READY'.

    Returns None if the string cannot be parsed.
    """
    text = text.strip()
    if not text:
        return None

    # match: field op value
    pattern = r"^(\w[\w.]*)\s*(>=|<=|!=|==|>|<)\s*(.+)$"
    m = re.match(pattern, text)
    if not m:
        return None

    field_name = m.group(1)
    op = m.group(2)
    raw_value = m.group(3).strip().strip("'\"")

    # try numeric
    try:
        value: Any = float(raw_value)
        if value == int(value):
            value = int(value)
    except ValueError:
        # boolean
        if raw_value.lower() in ("true", "false"):
            value = raw_value.lower() == "true"
        else:
            value = raw_value

    return ScreenCondition(
        field_name=field_name,
        op=op,
        value=value,
        description=text,
    )


def screen_stocks(
    candidates: Sequence[Mapping[str, Any]],
    conditions: Sequence[ScreenCondition],
    limit: int = 50,
) -> ScreenResult:
    """Filter candidates by all conditions (AND logic).

    Parameters
    ----------
    candidates : list of stock data dicts, each should include at minimum:
        symbol, name, and any fields referenced by conditions
    conditions : filter conditions (all must pass)
    limit : max results to return

    Returns
    -------
    ScreenResult with matched stocks.
    """
    warnings: list[str] = []
    matched: list[dict[str, Any]] = []

    if not conditions:
        warnings.append("没有设置筛选条件")
        return ScreenResult(
            matched=[],
            total_scanned=len(candidates),
            total_matched=0,
            conditions=list(conditions),
            warnings=warnings,
        )

    missing_counts: dict[str, int] = {}
    truncated = False
    for row in candidates:
        for condition in conditions:
            if _get_nested_value(row, condition.field_name) is None:
                missing_counts[condition.field_name] = missing_counts.get(condition.field_name, 0) + 1
        if all(_check_condition(row, c) for c in conditions):
            if len(matched) >= limit:
                truncated = True
            else:
                matched.append(dict(row))
    if truncated:
        warnings.append(f"结果超过 {limit} 条，已截断")
    for field_name, count in sorted(missing_counts.items()):
        if count:
            warnings.append(f"excluded_by_missing_field:{field_name}:{count}_stocks")

    return ScreenResult(
        matched=matched,
        total_scanned=len(candidates),
        total_matched=len(matched),
        conditions=list(conditions),
        warnings=warnings,
    )


def screen_result_to_markdown(result: ScreenResult) -> str:
    """Render screen result as Markdown."""
    lines: list[str] = []
    lines.append("# 选股筛选结果")
    lines.append("")
    lines.append(f"> {SCREENER_DISCLAIMER}")
    lines.append("")
    lines.append(f"扫描 {result.total_scanned} 只，命中 {result.total_matched} 只")
    lines.append("")

    if result.conditions:
        lines.append("## 筛选条件")
        for c in result.conditions:
            lines.append(f"- `{c.field_name} {c.op} {c.value}`")
        lines.append("")

    if result.warnings:
        for w in result.warnings:
            lines.append(f"⚠️ {w}")
        lines.append("")

    if not result.matched:
        lines.append("没有符合条件的股票。")
        return "\n".join(lines)

    lines.append("## 候选列表")
    lines.append("| 代码 | 名称 | 数据质量 |")
    lines.append("|:---|:---|:---|")
    for row in result.matched:
        symbol = row.get("symbol", "")
        name = row.get("name", "")
        grade = row.get("data_grade", "")
        lines.append(f"| {symbol} | {name} | {grade} |")

    lines.append("")
    lines.append(f"*{SCREENER_DISCLAIMER}*")
    return "\n".join(lines)
