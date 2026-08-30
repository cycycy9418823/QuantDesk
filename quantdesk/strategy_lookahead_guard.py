"""Strategy lookahead and intraday-proxy diagnostics.

This module is intentionally read-only.  It inspects already-produced strategy
inputs and execution payloads for common correctness risks: post-target data,
global statistics, daily-bar intraday proxies, and reliable fills without
minute data.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from quantdesk.shared import now_text, short_error_text

DISCLAIMER = "仅用于本地学习研究，不构成投资建议。"

# Keys whose value is the date of the underlying *data row* (what the row is
# "about"). Only these may trigger future-date / look-ahead detection.
CONTENT_DATE_KEYS = ("date", "datetime", "trade_date", "signal_date")

# Keys that are produced-artifact / bookkeeping timestamps (when the report or
# payload was generated or fetched), NOT the date of the underlying data. A
# future ``generated_at`` just means "this report was written today"; it must
# not be mistaken for look-ahead leakage.
ARTIFACT_TIMESTAMP_KEYS = ("generated_at", "retrieved_at", "target_date")

# Union kept for row-detection heuristics (deciding whether a dict is a record
# worth inspecting). Detection of *future* dates uses CONTENT_DATE_KEYS only.
DATE_KEYS = CONTENT_DATE_KEYS + ARTIFACT_TIMESTAMP_KEYS


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", text)
    if match:
        text = match.group(0).replace("/", "-")
    else:
        text = text[:10].replace("/", "-")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


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
        if any(key in value for key in DATE_KEYS) or any(key in value for key in ("fill_model", "trigger_source", "action")):
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
    rows: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            rows.extend(_walk(item, path=f"{path}.{key}"))
        return rows
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            rows.extend(_walk(item, path=f"{path}[{index}]"))
        return rows
    rows.append((path, value))
    return rows


def _row_date(row: dict[str, Any]) -> tuple[str, date | None]:
    # Only content-date keys drive future-date detection; artifact timestamps
    # (generated_at/retrieved_at) describe when the payload was produced, not
    # the data it contains, and must not be treated as look-ahead leakage.
    for key in CONTENT_DATE_KEYS:
        if key in row and row.get(key):
            parsed = _parse_date(row.get(key))
            if parsed:
                return key, parsed
    return "", None


def _is_forward_plan(row: dict[str, Any]) -> bool:
    """Return True when the record is a forward conditional plan.

    Conditional-execution plans carry a ``plan_id`` (e.g.
    ``plan_20260702_for_20260703``) and are produced on the current day to be
    executed on a later trading day. Their future ``trade_date`` is an intended
    schedule, not historical look-ahead leakage.
    """
    for key in ("plan_id", "plan_key"):
        if str(row.get(key) or "").strip():
            return True
    return False


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


def build_strategy_timeline_context(
    *,
    target_date: str,
    inputs: Any = None,
    features: Any = None,
    signals: Any = None,
    executions: Any = None,
    provider_payload: Any = None,
    intraday_status: Any = None,
    report_payload: Any = None,
) -> dict[str, Any]:
    """Collect read-only payloads into a validation context."""

    return {
        "target_date": target_date,
        "inputs": inputs if inputs is not None else {},
        "features": features if features is not None else {},
        "signals": signals if signals is not None else {},
        "executions": executions if executions is not None else {},
        "provider_payload": provider_payload if provider_payload is not None else {},
        "intraday_status": intraday_status if intraday_status is not None else {},
        "report_payload": report_payload if report_payload is not None else {},
    }


def detect_future_dates_in_inputs(inputs: Any, target_date: str, *, label: str = "inputs") -> list[dict[str, Any]]:
    """Find rows whose own date is after the target date."""

    target = _parse_date(target_date)
    if not target:
        return [_issue("invalid_target_date", f"target_date is invalid: {target_date}", path=label)]
    issues: list[dict[str, Any]] = []
    for path, row in _records(inputs, path=label):
        key, parsed = _row_date(row)
        if parsed and parsed > target:
            # A record carrying a ``plan_id`` is a forward conditional plan:
            # it is produced today to be executed on a later trading day, so a
            # future ``trade_date`` is an expected schedule, not historical
            # look-ahead leakage. Surface it as an info-level process-timing
            # note instead of an error.
            if _is_forward_plan(row):
                issues.append(
                    _issue(
                        "forward_plan_date",
                        f"{path}.{key}={parsed.isoformat()} is a forward plan date after target_date={target.isoformat()} (schedule, not look-ahead)",
                        severity="info",
                        path=f"{path}.{key}",
                        value=parsed.isoformat(),
                    )
                )
                continue
            issues.append(
                _issue(
                    "future_date_in_input",
                    f"{path}.{key}={parsed.isoformat()} is after target_date={target.isoformat()}",
                    path=f"{path}.{key}",
                    value=parsed.isoformat(),
                )
            )
    return issues


def detect_post_target_rows(payload: Any, target_date: str, *, label: str = "payload") -> list[dict[str, Any]]:
    """Alias for post-target row detection used by reports and provider traces."""

    return [
        {**issue, "type": "post_target_row"}
        for issue in detect_future_dates_in_inputs(payload, target_date, label=label)
    ]


def detect_target_date_leakage(context: dict[str, Any]) -> list[dict[str, Any]]:
    target_date = str(context.get("target_date") or "")
    issues: list[dict[str, Any]] = []
    for key in ("inputs", "features", "signals", "executions", "provider_payload", "report_payload"):
        issues.extend(detect_post_target_rows(context.get(key), target_date, label=key))
    return issues


def detect_global_stat_leakage(features: Any) -> list[dict[str, Any]]:
    """Detect feature metadata that indicates full-sample or global statistics."""

    suspicious = (
        "global_mean",
        "global_std",
        "global_min",
        "global_max",
        "full_sample",
        "whole_sample",
        "all_history_mean",
        "expanding_no_shift",
        "rolling_no_shift",
        "future_window",
    )
    issues: list[dict[str, Any]] = []
    for path, value in _walk(features, path="features"):
        text = f"{path}={value}".lower()
        if any(marker in text for marker in suspicious):
            issues.append(
                _issue(
                    "global_stat_leakage",
                    "feature metadata suggests a global or unshifted statistic that may see future rows",
                    severity="warning",
                    path=path,
                    value=value,
                )
            )
        if path.lower().endswith(("use_global_stats", "uses_global_stats")) and bool(value):
            issues.append(
                _issue(
                    "global_stat_leakage",
                    "feature explicitly declares global statistics",
                    severity="warning",
                    path=path,
                    value=value,
                )
            )
    return issues


def detect_daily_bar_intraday_proxy(payload: Any) -> list[dict[str, Any]]:
    """Detect daily OHLC values used as intraday trigger/fill proxies."""

    markers = (
        "daily_high_low",
        "daily_bar",
        "daily_high",
        "daily_low",
        "daily_close",
        "use_daily_high_low",
        "uses_daily_high_low",
        "intraday_proxy",
        "signal_uses_close",
    )
    issues: list[dict[str, Any]] = []
    for path, value in _walk(payload, path="payload"):
        text = f"{path}={value}".lower()
        if any(marker in text for marker in markers):
            issues.append(
                _issue(
                    "daily_bar_intraday_proxy",
                    "daily bar high/low/close appears to be used as an intraday trigger or fill proxy",
                    path=path,
                    value=value,
                )
            )
    return issues


def _detect_reliable_without_intraday(context: dict[str, Any]) -> list[dict[str, Any]]:
    intraday_status = context.get("intraday_status") if isinstance(context.get("intraday_status"), dict) else {}
    missing = bool(
        intraday_status.get("missing_intraday")
        or intraday_status.get("cannot_verify_intraday_trigger")
        or intraday_status.get("status") in {"missing_intraday", "cannot_verify_intraday_trigger"}
    )
    issues: list[dict[str, Any]] = []
    for path, row in _records(context.get("executions"), path="executions"):
        action = str(row.get("action") or row.get("actual_action") or "").lower()
        fill_model = str(row.get("fill_model") or row.get("actual_fill_model") or "").lower()
        reliable = bool(row.get("is_reliable") or row.get("reliable") or row.get("reliable_simulated_event"))
        if missing and reliable and action not in {"", "no_fill"}:
            issues.append(
                _issue(
                    "reliable_execution_without_intraday",
                    "execution is marked reliable while intraday data is missing or cannot verify triggers",
                    path=path,
                    value=fill_model or action,
                )
            )
        if fill_model == "missing_intraday" and action not in {"", "no_fill"}:
            issues.append(
                _issue(
                    "missing_intraday_marked_as_fill",
                    "missing_intraday execution must not be treated as a fill",
                    path=path,
                    value=action,
                )
            )
    return issues


def validate_strategy_no_lookahead(
    context: dict[str, Any] | None = None,
    *,
    target_date: str = "",
    inputs: Any = None,
    features: Any = None,
    signals: Any = None,
    executions: Any = None,
    provider_payload: Any = None,
    intraday_status: Any = None,
    report_payload: Any = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Run all lookahead diagnostics and return a compact result."""

    ctx = dict(context or {})
    if not ctx:
        ctx = build_strategy_timeline_context(
            target_date=target_date,
            inputs=inputs,
            features=features,
            signals=signals,
            executions=executions,
            provider_payload=provider_payload,
            intraday_status=intraday_status,
            report_payload=report_payload,
        )
    target = str(ctx.get("target_date") or target_date or "")
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for issue in detect_target_date_leakage(ctx):
        issues.append(issue)
    for issue in detect_global_stat_leakage(ctx.get("features")):
        warnings.append(issue)
    for source_key in ("signals", "executions", "report_payload", "features"):
        for issue in detect_daily_bar_intraday_proxy(ctx.get(source_key)):
            issues.append({**issue, "path": f"{source_key}.{issue.get('path', '')}"})
    issues.extend(_detect_reliable_without_intraday(ctx))

    # ``info``-level issues (e.g. forward_plan_date) are process-timing notes,
    # not correctness problems: they must not push the guard into failed/serious
    # nor drag the score down. Only error/critical severities are "serious".
    serious_count = sum(
        1 for issue in issues if issue.get("severity") not in {"warning", "info"}
    )
    info_count = sum(1 for issue in issues if issue.get("severity") == "info")
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
        "target_date": target,
        "issue_count": len(issues),
        "info_count": info_count,
        "issues": issues,
        "warnings": warnings,
        "lookahead_score": score,
        "checked_at": now_text(),
        "strict": bool(strict),
        "disclaimer": DISCLAIMER,
    }


def summarize_lookahead_issues(result: dict[str, Any]) -> str:
    status = result.get("status") or "unknown"
    issue_count = int(result.get("issue_count") or 0)
    warning_count = len(result.get("warnings") or [])
    return f"lookahead_status={status}, issues={issue_count}, warnings={warning_count}, score={result.get('lookahead_score')}"
