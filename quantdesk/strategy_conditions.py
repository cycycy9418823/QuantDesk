from __future__ import annotations

from typing import Any, Mapping

from quantdesk.utils import format_number, safe_float


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nested_get(data: Mapping[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for part in str(path or "").split("."):
        if not part:
            continue
        mapping = _mapping(current)
        if part not in mapping:
            return default
        current = mapping[part]
    return current


def _condition_text(condition: Mapping[str, Any]) -> str:
    field = str(condition.get("field") or "")
    op = str(condition.get("op") or "==")
    explain = str(condition.get("explain") or field or "未知条件")
    if condition.get("explain"):
        return explain
    if condition.get("target_field"):
        target = str(condition.get("target_field") or "")
        multiplier = safe_float(condition.get("multiplier"), 1.0) or 1.0
        suffix = f" * {format_number(multiplier)}" if multiplier != 1 else ""
        if op in {"cross_above", "cross_below"}:
            return explain
        return f"{field} {op} {target}{suffix}"
    if condition.get("lower") is not None or condition.get("upper") is not None:
        return f"{field} {op} 区间"
    if op in {"lookback_high", "lookback_low", "near_pct"}:
        return explain
    return f"{field} {op} {format_number(condition.get('value'))}"


def _field_label(field: str) -> str:
    return str(field or "未知字段")


def _missing_result(
    condition: Mapping[str, Any],
    field: str,
    current_value: Any = "",
    target_value: Any = "",
    explain: str | None = None,
) -> dict[str, Any]:
    return {
        "matched": False,
        "missing": True,
        "condition_result": "missing",
        "display_condition": _condition_text(condition),
        "current_value": format_number(current_value) if current_value not in {None, ""} else "",
        "target_value": format_number(target_value) if target_value not in {None, ""} else "",
        "explain": explain or f"缺少{_field_label(field)}数据，无法检查该条件。",
    }


def _current_and_target(data: Mapping[str, Any], condition: Mapping[str, Any]) -> tuple[Any, Any, str]:
    field = str(condition.get("field") or "")
    target_field = str(condition.get("target_field") or "")
    current_raw = _nested_get(data, field)
    if current_raw in {None, ""}:
        return None, None, field
    if target_field:
        target_raw = _nested_get(data, target_field)
        if target_raw in {None, ""}:
            return current_raw, None, target_field
        multiplier = safe_float(condition.get("multiplier"), 1.0) or 1.0
        target_value = safe_float(target_raw)
        if target_value is not None:
            target_value *= multiplier
        return current_raw, target_value, target_field
    if condition.get("between") and isinstance(condition.get("between"), (list, tuple)):
        return current_raw, condition.get("between"), field
    if condition.get("lower") is not None or condition.get("upper") is not None:
        return current_raw, (condition.get("lower"), condition.get("upper")), field
    if condition.get("lookback") is not None:
        return current_raw, condition.get("lookback"), field
    return current_raw, condition.get("value"), field


def _previous_value(data: Mapping[str, Any], condition: Mapping[str, Any], key: str, role: str) -> tuple[float | None, str]:
    explicit_key = str(condition.get(f"previous_{role}_field") or "")
    candidates = []
    if explicit_key:
        candidates.append(explicit_key)
    if key:
        candidates.append(f"previous_{key}")
    for candidate in candidates:
        value = safe_float(_nested_get(data, candidate))
        if value is not None:
            return value, candidate
    return None, (candidates[0] if candidates else f"previous_{key}")


def _cross_result(
    data: Mapping[str, Any],
    current_value: float,
    target_raw: Any,
    op: str,
    field: str,
    condition: Mapping[str, Any],
) -> dict[str, Any]:
    target_field = str(condition.get("target_field") or "")
    target_value = safe_float(target_raw)
    if target_value is None:
        return _missing_result(condition, target_field or field, current_value, "", f"缺少{_field_label(target_field or field)}数据，无法判断穿越条件。")

    previous_current, previous_current_key = _previous_value(data, condition, field, "field")
    previous_target, previous_target_key = _previous_value(data, condition, target_field, "target")
    if previous_current is None:
        return _missing_result(
            condition,
            previous_current_key,
            current_value,
            target_value,
            f"缺少前值字段 {previous_current_key}，无法判断是否发生穿越。",
        )
    if previous_target is None:
        return _missing_result(
            condition,
            previous_target_key,
            current_value,
            target_value,
            f"缺少前值字段 {previous_target_key}，无法判断是否发生穿越。",
        )

    if op == "cross_above":
        matched = previous_current <= previous_target and current_value > target_value
    else:
        matched = previous_current >= previous_target and current_value < target_value
    return {
        "matched": matched,
        "missing": False,
        "condition_result": "matched" if matched else "failed",
        "display_condition": _condition_text(condition),
        "current_value": format_number(current_value),
        "target_value": format_number(target_value),
        "explain": str(condition.get("explain") or _condition_text(condition)),
        "previous_value": format_number(previous_current),
        "previous_target_value": format_number(previous_target),
    }


def _numeric_result(
    data: Mapping[str, Any],
    current_raw: Any,
    target_raw: Any,
    op: str,
    field: str,
    condition: Mapping[str, Any],
) -> dict[str, Any]:
    current_value = safe_float(current_raw)
    if current_value is None:
        return _missing_result(condition, field)

    if op == "between":
        bounds = target_raw if isinstance(target_raw, (list, tuple)) else (None, None)
        low = safe_float(bounds[0]) if len(bounds) > 0 else None
        high = safe_float(bounds[1]) if len(bounds) > 1 else None
        if low is None or high is None:
            return _missing_result(condition, field, current_value, "", f"{_field_label(field)}的上下界数据不完整，无法检查该条件。")
        matched = low <= current_value <= high
        return {
            "matched": matched,
            "missing": False,
            "condition_result": "matched" if matched else "failed",
            "display_condition": _condition_text(condition),
            "current_value": format_number(current_value),
            "target_value": f"{format_number(low)} ~ {format_number(high)}",
            "explain": str(condition.get("explain") or _condition_text(condition)),
        }

    if op in {"lookback_high", "lookback_low"}:
        target_value = safe_float(target_raw)
        if target_value is None:
            return _missing_result(condition, field, current_value)
        matched = current_value >= target_value if op == "lookback_high" else current_value <= target_value
        label = "近期高点" if op == "lookback_high" else "近期低点"
        return {
            "matched": matched,
            "missing": False,
            "condition_result": "matched" if matched else "failed",
            "display_condition": _condition_text(condition),
            "current_value": format_number(current_value),
            "target_value": f"{label} {format_number(target_value)}",
            "explain": str(condition.get("explain") or _condition_text(condition)),
        }

    if op == "near_pct":
        target_value = safe_float(target_raw)
        if target_value is None:
            return _missing_result(condition, field, current_value, "", f"缺少{_field_label(field)}目标数据，无法判断是否接近目标区间。")
        pct = safe_float(condition.get("value"), 5.0) or 5.0
        low = target_value * (1 - pct / 100)
        high = target_value * (1 + pct / 100)
        matched = low <= current_value <= high
        return {
            "matched": matched,
            "missing": False,
            "condition_result": "matched" if matched else "failed",
            "display_condition": _condition_text(condition),
            "current_value": format_number(current_value),
            "target_value": f"{format_number(low)} ~ {format_number(high)}",
            "explain": str(condition.get("explain") or _condition_text(condition)),
        }

    if op in {"cross_above", "cross_below"}:
        return _cross_result(data, current_value, target_raw, op, str(condition.get("field") or field), condition)

    target_value = safe_float(target_raw) if target_raw is not None and not isinstance(target_raw, str) else target_raw
    if target_value is None and target_raw not in {None, ""}:
        target_value = target_raw
    try:
        if op == ">":
            matched = current_value > float(target_value)
        elif op == ">=":
            matched = current_value >= float(target_value)
        elif op == "<":
            matched = current_value < float(target_value)
        elif op == "<=":
            matched = current_value <= float(target_value)
        elif op == "==":
            matched = current_value == float(target_value)
        elif op == "!=":
            matched = current_value != float(target_value)
        else:
            matched = False
    except (TypeError, ValueError):
        return _missing_result(condition, field, current_value, target_value, f"{_field_label(field)}数值无法转换，无法完成条件检查。")

    return {
        "matched": matched,
        "missing": False,
        "condition_result": "matched" if matched else "failed",
        "display_condition": _condition_text(condition),
        "current_value": format_number(current_value),
        "target_value": format_number(target_value),
        "explain": str(condition.get("explain") or _condition_text(condition)),
    }


def evaluate_condition(row: Mapping[str, Any], condition: Mapping[str, Any]) -> dict[str, Any]:
    current_raw, target_raw, field = _current_and_target(row, condition)
    op = str(condition.get("op") or "==")
    result = _numeric_result(row, current_raw, target_raw, op, field, condition)
    result.setdefault("field", field)
    result.setdefault("condition_field", str(condition.get("field") or ""))
    result.setdefault("target_field", str(condition.get("target_field") or ""))
    return result


def evaluate_conditions(
    row: Mapping[str, Any],
    conditions: list[Mapping[str, Any]],
    *,
    mode: str = "all",
) -> dict[str, Any]:
    """Evaluate a list of conditions against one row.

    mode="all" (default): ``triggered`` is true only when every condition
    matches (no failures, no missing data) — correct semantics for *entry*.
    mode="any": ``triggered`` is true as soon as one condition matches —
    correct semantics for *exit* (跌破均线 OR 止损 OR 止盈 OR ...), so a
    position is not held until the unlikely AND of all exit rules occurs.

    ``all_matched`` is retained unchanged for backward compatibility; new
    callers should read ``triggered`` (which respects ``mode``).
    """
    matched: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for condition in conditions or []:
        result = evaluate_condition(row, condition)
        results.append(result)
        compact = {
            "field": str(result.get("field") or condition.get("field") or ""),
            "target_field": str(condition.get("target_field") or ""),
            "condition": _condition_text(condition),
            "display_condition": result.get("display_condition", _condition_text(condition)),
            "explain": result.get("explain", ""),
            "current_value": result.get("current_value", ""),
            "target_value": result.get("target_value", ""),
        }
        if result.get("missing"):
            missing.append(compact)
        elif result.get("matched"):
            matched.append(compact)
        else:
            failed.append(compact)
    all_matched = bool(results) and not failed and not missing
    any_matched = bool(matched)
    triggered = any_matched if str(mode).lower() == "any" else all_matched
    return {
        "matched": matched,
        "failed": failed,
        "missing": missing,
        "condition_results": results,
        "all_matched": all_matched,
        "any_matched": any_matched,
        "triggered": triggered,
        "mode": str(mode).lower(),
    }


__all__ = ["evaluate_condition", "evaluate_conditions"]
