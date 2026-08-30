"""Execution assumption diagnostics for paper simulation and backtests."""

from __future__ import annotations

from typing import Any

from quantdesk.backtest import default_backtest_assumptions
from quantdesk.shared import now_text, short_error_text

DISCLAIMER = "仅用于纸面模拟和学习观察，不代表真实可成交。"


def _records(value: Any, *, path: str = "input") -> list[tuple[str, dict[str, Any]]]:
    if value is None:
        return []
    if hasattr(value, "to_dict") and not isinstance(value, dict):
        try:
            rows = value.to_dict(orient="records")
            return [(f"{path}[{index}]", row) for index, row in enumerate(rows) if isinstance(row, dict)]
        except Exception:
            return []
    if isinstance(value, dict):
        if any(key in value for key in ("action", "actual_action", "fill_model", "actual_fill_model", "cash_delta")):
            return [(path, value)]
        rows: list[tuple[str, dict[str, Any]]] = []
        for key, item in value.items():
            rows.extend(_records(item, path=f"{path}.{key}"))
        return rows
    if isinstance(value, (list, tuple)):
        rows = []
        for index, item in enumerate(value):
            rows.extend(_records(item, path=f"{path}[{index}]"))
        return rows
    return []


def _walk(value: Any, *, path: str = "input") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        rows: list[tuple[str, Any]] = []
        for key, item in value.items():
            rows.extend(_walk(item, path=f"{path}.{key}"))
        return rows
    if isinstance(value, (list, tuple)):
        rows = []
        for index, item in enumerate(value):
            rows.extend(_walk(item, path=f"{path}[{index}]"))
        return rows
    return [(path, value)]


def _issue(issue_type: str, message: str, *, severity: str = "error", path: str = "", value: Any = None) -> dict[str, Any]:
    payload = {
        "type": issue_type,
        "severity": severity,
        "message": message,
        "path": path,
    }
    if value is not None:
        payload["value"] = short_error_text(value)
    return payload


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _action(row: dict[str, Any]) -> str:
    return str(row.get("action") or row.get("actual_action") or row.get("event") or "").strip().lower()


def _fill_model(row: dict[str, Any]) -> str:
    return str(row.get("fill_model") or row.get("actual_fill_model") or row.get("execution_model") or "").strip().lower()


def detect_limit_up_fill_assumption(execution_payload: Any) -> list[dict[str, Any]]:
    """Flag fills at or near limit-up prices."""

    issues: list[dict[str, Any]] = []
    for path, row in _records(execution_payload, path="execution"):
        action = _action(row)
        if action not in {"buy", "open", "entry"}:
            continue
        fill_price = _num(row.get("fill_price") or row.get("actual_trigger_price") or row.get("price"))
        limit_up = _num(row.get("limit_up_price") or row.get("price_limit_up") or row.get("涨停价"))
        text = " ".join(str(row.get(key) or "") for key in ("reason", "tag", "candidate_type", "strategy", "note")).lower()
        near_limit = bool(fill_price is not None and limit_up and abs(fill_price - limit_up) / max(limit_up, 0.01) <= 0.002)
        if near_limit or "limit_up" in text or "board" in text or "打板" in text or "涨停" in text:
            issues.append(
                _issue(
                    "limit_up_fill_assumption",
                    "paper fill is at or near limit-up; real market queue execution may be unavailable",
                    severity="warning",
                    path=path,
                    value=row,
                )
            )
    return issues


def detect_daily_high_low_fill_assumption(execution_payload: Any) -> list[dict[str, Any]]:
    markers = ("daily_high_low", "daily_bar", "daily_high", "daily_low", "日线最高", "日线最低")
    issues: list[dict[str, Any]] = []
    for path, value in _walk(execution_payload, path="execution"):
        text = f"{path}={value}".lower()
        if any(marker.lower() in text for marker in markers):
            issues.append(
                _issue(
                    "daily_high_low_fill_assumption",
                    "daily high/low appears to be used as a fill assumption; minute data is required for intraday verification",
                    path=path,
                    value=value,
                )
            )
    return issues


def detect_missing_intraday_fill(execution_payload: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path, row in _records(execution_payload, path="execution"):
        action = _action(row)
        fill_model = _fill_model(row)
        status = str(row.get("status") or row.get("trigger_status") or "").lower()
        reliable = bool(row.get("is_reliable") or row.get("reliable_simulated_event") or row.get("reliable"))
        path_marks_unfilled = ".no_fill" in path or ".missing_intraday" in path
        if path_marks_unfilled:
            continue
        if (fill_model == "missing_intraday" or status == "missing_intraday") and action not in {"", "no_fill"}:
            issues.append(
                _issue(
                    "missing_intraday_fill",
                    "missing_intraday must not be recorded as a filled event",
                    path=path,
                    value=row,
                )
            )
        if (fill_model == "missing_intraday" or status == "missing_intraday") and reliable:
            issues.append(
                _issue(
                    "missing_intraday_reliable_execution",
                    "missing_intraday cannot be marked reliable",
                    path=path,
                    value=row,
                )
            )
    return issues


def detect_board_candidate_fill_risk(execution_payload: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path, row in _records(execution_payload, path="execution"):
        text = " ".join(str(value) for value in row.values()).lower()
        if row.get("board_candidate") or row.get("is_board_candidate") or "打板" in text or "limit_up" in text:
            issues.append(
                _issue(
                    "board_candidate_fill_risk",
                    "board-candidate paper fill cannot represent real limit-up queue execution",
                    severity="warning",
                    path=path,
                    value=row,
                )
            )
    return issues


def _position_delta(row: dict[str, Any]) -> float:
    for key in ("position_delta", "quantity_delta", "shares_delta"):
        value = _num(row.get(key))
        if value is not None:
            return value
    before = _num(row.get("position_before") or row.get("quantity_before"))
    after = _num(row.get("position_after") or row.get("quantity_after"))
    if before is not None and after is not None:
        return after - before
    return 0.0


def _cash_delta(row: dict[str, Any]) -> float:
    value = _num(row.get("cash_delta"))
    if value is not None:
        return value
    before = _num(row.get("cash_before"))
    after = _num(row.get("cash_after"))
    if before is not None and after is not None:
        return after - before
    return 0.0


def detect_no_fill_state_change(execution_payload: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path, row in _records(execution_payload, path="execution"):
        action = _action(row)
        fill_model = _fill_model(row)
        status = str(row.get("status") or row.get("trigger_status") or "").lower()
        is_no_fill = action == "no_fill" or fill_model in {"no_fill", "missing_intraday"} or status in {"no_fill", "missing_intraday"}
        if is_no_fill and (abs(_cash_delta(row)) > 1e-9 or abs(_position_delta(row)) > 1e-9):
            issues.append(
                _issue(
                    "no_fill_state_change",
                    "no_fill or missing_intraday event changed cash or holdings",
                    path=path,
                    value=row,
                )
            )
    return issues


def detect_tplus1_violation(execution_payload: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path, row in _records(execution_payload, path="execution"):
        action = _action(row)
        if action not in {"sell", "reduce_half", "stop_loss", "sell_all"}:
            continue
        buy_date = str(row.get("buy_date") or row.get("entry_date") or "").strip()[:10]
        sell_date = str(row.get("sell_date") or row.get("trade_date") or row.get("date") or "").strip()[:10]
        sellable = _num(row.get("sellable_quantity"))
        quantity = _num(row.get("quantity") or row.get("actual_quantity"))
        if row.get("tplus1_violation") or (buy_date and sell_date and buy_date == sell_date) or (
            sellable is not None and quantity is not None and quantity > sellable
        ):
            issues.append(
                _issue(
                    "tplus1_violation",
                    "A-share T+1 sellability appears to be violated",
                    path=path,
                    value=row,
                )
            )
    return issues


def _assumption_warnings(assumptions: dict[str, Any] | None) -> list[dict[str, Any]]:
    payload = assumptions if isinstance(assumptions, dict) else default_backtest_assumptions()
    if payload.get("board_candidate_execution"):
        return []
    return [
        _issue(
            "missing_board_candidate_execution_assumption",
            "assumptions should include board_candidate_execution transparency text",
            severity="warning",
            path="assumptions.board_candidate_execution",
        )
    ]


def validate_execution_assumptions(
    execution_payload: Any = None,
    *,
    assumptions: dict[str, Any] | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    issues.extend(detect_daily_high_low_fill_assumption(execution_payload))
    issues.extend(detect_missing_intraday_fill(execution_payload))
    issues.extend(detect_no_fill_state_change(execution_payload))
    issues.extend(detect_tplus1_violation(execution_payload))
    warnings.extend(detect_limit_up_fill_assumption(execution_payload))
    warnings.extend(detect_board_candidate_fill_risk(execution_payload))
    warnings.extend(_assumption_warnings(assumptions))

    serious_count = len(issues)
    warning_count = len(warnings)
    status = "passed"
    if serious_count:
        status = "failed" if strict else "warning"
    elif warning_count:
        status = "warning"
    score = max(0.0, round(1.0 - serious_count * 0.25 - warning_count * 0.05, 4))
    return {
        "ok": status != "failed",
        "status": status,
        "issue_count": len(issues),
        "issues": issues,
        "warnings": warnings,
        "execution_assumption_score": score,
        "checked_at": now_text(),
        "strict": bool(strict),
        "disclaimer": DISCLAIMER,
    }


def summarize_execution_assumption_issues(result: dict[str, Any]) -> str:
    status = result.get("status") or "unknown"
    issue_count = int(result.get("issue_count") or 0)
    warning_count = len(result.get("warnings") or [])
    return (
        f"execution_assumption_status={status}, "
        f"issues={issue_count}, warnings={warning_count}, score={result.get('execution_assumption_score')}"
    )
