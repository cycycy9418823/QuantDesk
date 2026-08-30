"""Proactive, de-duplicated local data alerts derived from scope health.

Workflow A3 of the 8000+8001 product closure needs a *pure* alert contract over
the per-scope verdicts produced by :mod:`quantdesk.data_scope_health`. It covers
scheduled-task failure / timeout / missed run / disabled task, research-pool
bars lagging or missing, unavailable AI-relevant symbols, non-formal minute
coverage, and recovery when a firing condition clears.

Design rules taken straight from the task book:

- Every alert carries a stable ``alert_key`` so one unchanged problem is
  reported once instead of piling up on each evaluation. The key deliberately
  excludes volatile counts; the ``dedup_key`` is the same value, exposed under
  the name a notifier would look for.
- Missing data is ``critical``; mere lag is ``warning``. They never share a
  ``reason_code``, so "no data" stays distinguishable from "old data".
- Only the AI scope may set ``blocks_ai_trader``. A stale *full universe* alone
  must not block the AI paper trader, because the AI scope can be complete
  while the whole tree lags — which is exactly the 2026-08-28 situation.
- Writing a local runtime file is **not** user notification. ``diff_alert_states``
  reports ``external_push=False`` / ``user_notified=False`` and a
  ``local_runtime_only`` channel; external delivery needs separate approval.

This module performs no file, network, ledger or LLM access. It maps an
in-memory health payload to alert dictionaries, which is what makes it callable
from a read-only 8000 GET route.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from quantdesk.data_scope_health import (
    COVERAGE_MISSING,
    COVERAGE_PARTIAL,
    COVERAGE_PROVISIONAL,
    SCOPE_AI_TRADER,
    SCOPE_MINUTE_SCHEDULER,
    STATUS_BLOCKED,
    STATUS_DEGRADED,
    STATUS_OK,
    STATUS_UNKNOWN,
    STATUS_WARNING,
)

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

#: Scope status -> alert severity. ``blocked`` is the only critical level:
#: unusable data is materially different from lagging data.
_SEVERITY_BY_STATUS: dict[str, str] = {
    STATUS_BLOCKED: SEVERITY_CRITICAL,
    STATUS_DEGRADED: SEVERITY_WARNING,
    STATUS_WARNING: SEVERITY_WARNING,
    STATUS_UNKNOWN: SEVERITY_WARNING,
}

#: Scheduler probe state -> (reason_code, severity). Each failure mode keeps a
#: distinct reason code so a crash, a hang, a skipped run and an operator
#: disabling the task are never conflated in the UI.
_SCHEDULER_REASONS: dict[str, tuple[str, str]] = {
    "last_run_failed": ("scheduler_task_failed", SEVERITY_CRITICAL),
    "failed": ("scheduler_task_failed", SEVERITY_CRITICAL),
    "timeout": ("scheduler_task_timeout", SEVERITY_CRITICAL),
    "timed_out": ("scheduler_task_timeout", SEVERITY_CRITICAL),
    "missed": ("scheduler_run_missed", SEVERITY_WARNING),
    "missing": ("scheduler_task_missing", SEVERITY_CRITICAL),
    "disabled": ("scheduler_task_disabled", SEVERITY_WARNING),
    "unmanaged": ("scheduler_task_unmanaged", SEVERITY_WARNING),
    "misconfigured": ("scheduler_task_misconfigured", SEVERITY_WARNING),
    "last_run_unknown": ("scheduler_result_unknown", SEVERITY_WARNING),
    "unavailable": ("scheduler_probe_unavailable", SEVERITY_WARNING),
    "not_supported": ("scheduler_probe_unavailable", SEVERITY_WARNING),
}

_NON_FORMAL_COVERAGE: dict[str, str] = {
    COVERAGE_MISSING: "minute_coverage_missing",
    COVERAGE_PARTIAL: "minute_coverage_partial",
    COVERAGE_PROVISIONAL: "minute_coverage_provisional",
}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _affected_summary(scope: Mapping[str, Any]) -> dict[str, Any]:
    """Auditable evidence for one alert: counts plus a bounded symbol sample."""

    return {
        "scope": _text(scope.get("scope")),
        "label": _text(scope.get("label")),
        "total_symbols": _int(scope.get("total_symbols")),
        "covered_symbols": _int(scope.get("covered_symbols")),
        "stale_symbols": _int(scope.get("stale_symbols")),
        "missing_symbols": _int(scope.get("missing_symbols")),
        "coverage_state": _text(scope.get("coverage_state")),
        "symbols_sample": list(scope.get("symbols_sample") or [])[:10],
    }


def _alert(
    *,
    scope: str,
    reason_code: str,
    severity: str,
    as_of: str,
    detail: str,
    affected: Mapping[str, Any],
    blocks_ai_trader: bool = False,
) -> dict[str, Any]:
    """Assemble one alert.

    ``alert_key`` is intentionally ``scope:reason_code`` only. Including counts
    or dates would mint a fresh key every time a number moved, which is exactly
    the re-notification storm the dedup requirement forbids.
    """

    alert_key = f"{scope}:{reason_code}"
    return {
        "alert_key": alert_key,
        "dedup_key": alert_key,
        "scope": scope,
        "reason_code": reason_code,
        "severity": severity,
        "as_of": as_of,
        "detail": detail,
        "affected": dict(affected),
        "blocks_ai_trader": bool(blocks_ai_trader),
        "resolved": False,
    }


def _scheduler_alerts(health: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Alerts for the scheduled-task side of the minute/scheduler scope.

    The scheduler verdict arrives as the flat payload built by
    ``data_scope_health.scheduler_scope_status()`` (``scheduler_state``,
    ``evidence_source``, ``last_exit_code``). A legacy nested ``scheduler``
    dict is still accepted so an older caller keeps working.

    Each failure mode keeps its own ``reason_code``: a task that failed, timed
    out, was missed or is disabled are four different operational problems and
    must not collapse into one generic "unhealthy" alert.
    """

    scope = (health.get("scopes") or {}).get(SCOPE_MINUTE_SCHEDULER) or {}
    nested = scope.get("scheduler") if isinstance(scope.get("scheduler"), dict) else {}
    # Prefer the flat A2 payload; fall back to a nested legacy block.
    source: Mapping[str, Any] = scope if scope.get("scheduler_state") else nested
    if not source:
        return []

    state = _text(source.get("scheduler_state") or source.get("state")).lower()
    if not state or state == "unknown":
        return []
    scope_status = _text(scope.get("status")).lower()
    scheduler_status = _text(scope.get("scheduler_status")).lower()
    if (scope_status == STATUS_OK and scheduler_status in {"", STATUS_OK}) or state in {
        "ok",
        "ready",
        "running_ok",
    }:
        return []

    reason_code, severity = _SCHEDULER_REASONS.get(
        state, ("scheduler_state_degraded", SEVERITY_WARNING)
    )
    # A non-zero exit code is hard evidence of failure regardless of the label
    # the probe used, so it is surfaced in the detail text.
    exit_code = source.get("last_exit_code")
    detail = _text(source.get("detail")) or f"计划任务状态：{state}"
    if exit_code not in (None, "", 0):
        detail = f"{detail}（退出码 {exit_code}）"

    return [
        _alert(
            scope=SCOPE_MINUTE_SCHEDULER,
            reason_code=reason_code,
            severity=severity,
            # A probe may carry no date of its own; the evaluation's target day
            # is then the honest "as of", never a blank field.
            as_of=(
                _text(scope.get("as_of"))
                or _text(source.get("as_of"))
                or _text(health.get("target_date"))
            ),
            detail=detail,
            affected={
                "scope": SCOPE_MINUTE_SCHEDULER,
                "task_name": _text(source.get("task_name")),
                "state": state,
                "evidence_source": _text(scope.get("evidence_source")),
                "last_exit_code": exit_code,
            },
        )
    ]


def _coverage_alerts(scope_name: str, scope: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Alerts for one data scope's coverage verdict."""

    status = _text(scope.get("status")).lower()
    if status == STATUS_OK:
        return []

    severity = _SEVERITY_BY_STATUS.get(status, SEVERITY_WARNING)
    # Only the AI scope may block the AI trader. A lagging full universe is
    # visible but must never gate new AI commitments.
    blocks_ai = scope_name == SCOPE_AI_TRADER and status in {STATUS_BLOCKED, STATUS_UNKNOWN}
    reason_code = _text(scope.get("reason_code")) or "scope_unhealthy"
    # The shared minute/scheduler display scope can contain a failed scheduler
    # *and* a separate minute-coverage verdict.  The scheduler-specific alert
    # below is the sole owner of scheduler failure reason codes; emitting it
    # here as well would duplicate one operational problem under two keys.
    scheduler_reason = _text(scope.get("scheduler_reason_code"))
    if scope_name == SCOPE_MINUTE_SCHEDULER and scheduler_reason and reason_code == scheduler_reason:
        coverage_status = _text(scope.get("coverage_state")).lower()
        if coverage_status in _NON_FORMAL_COVERAGE:
            coverage_reason = _NON_FORMAL_COVERAGE[coverage_status]
            return [
                _alert(
                    scope=scope_name,
                    reason_code=coverage_reason,
                    severity=(
                        SEVERITY_CRITICAL
                        if coverage_status == COVERAGE_MISSING
                        else SEVERITY_WARNING
                    ),
                    as_of=_text(scope.get("as_of")) or _text(scope.get("target_date")),
                    detail=(
                        f"分钟数据为 {coverage_status}；按 docs/price_data_contract.md "
                        "不得用于正式回测、特征或执行。"
                    ),
                    affected=_affected_summary(scope),
                )
            ]
        return []
    as_of = _text(scope.get("as_of"))
    affected = _affected_summary(scope)

    alerts = [
        _alert(
            scope=scope_name,
            reason_code=reason_code,
            severity=severity,
            # An unobserved scope has no date of its own; fall back to the
            # target day so the alert still carries a required ``as_of``.
            as_of=as_of or _text(scope.get("target_date")),
            detail=_text(scope.get("detail")) or f"{scope.get('label') or scope_name} 状态为 {status}。",
            affected=affected,
            blocks_ai_trader=blocks_ai,
        )
    ]

    # Non-formal minute coverage is a separate fact from the scope verdict:
    # provisional bars may look "present" yet must not enter formal use.
    coverage_state = _text(scope.get("coverage_state")).lower()
    extra_reason = _NON_FORMAL_COVERAGE.get(coverage_state)
    if extra_reason and extra_reason != reason_code:
        alerts.append(
            _alert(
                scope=scope_name,
                reason_code=extra_reason,
                severity=SEVERITY_CRITICAL if coverage_state == COVERAGE_MISSING else SEVERITY_WARNING,
                as_of=as_of or _text(scope.get("target_date")),
                detail=(
                    f"分钟数据为 {coverage_state}；按 docs/price_data_contract.md "
                    "不得用于正式回测、特征或执行。"
                ),
                affected=affected,
                blocks_ai_trader=False,
            )
        )
    return alerts


def build_data_alerts(health: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive the current alert set from a scope-health payload.

    Returns ``[]`` on a genuinely healthy day: silence is meaningful, so no
    filler "everything is fine" alert is emitted. Ordering follows the scope
    declaration order, keeping repeated evaluations byte-stable.
    """

    scopes = health.get("scopes") if isinstance(health.get("scopes"), dict) else {}
    alerts: list[dict[str, Any]] = []
    seen: set[str] = set()

    for scope_name, scope in scopes.items():
        if not isinstance(scope, Mapping):
            continue
        for alert in _coverage_alerts(scope_name, scope):
            if alert["alert_key"] not in seen:
                seen.add(alert["alert_key"])
                alerts.append(alert)

    for alert in _scheduler_alerts(health):
        if alert["alert_key"] not in seen:
            seen.add(alert["alert_key"])
            alerts.append(alert)

    return alerts


def _as_key_map(alerts: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {_text(alert.get("alert_key")): dict(alert) for alert in alerts if alert.get("alert_key")}


def _recovery_event(alert: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a cleared alert into an explicit recovery event.

    A condition that simply disappears from the payload is invisible, so the
    previously firing key is re-emitted once with ``resolved=True`` at
    ``info`` severity.
    """

    recovered = dict(alert)
    recovered["resolved"] = True
    recovered["severity"] = SEVERITY_INFO
    recovered["detail"] = f"已恢复：{_text(alert.get('detail'))}".strip("：")
    recovered["recovered_from_reason_code"] = _text(alert.get("reason_code"))
    return recovered


def diff_alert_states(
    *,
    previous: Sequence[Mapping[str, Any]] | None = None,
    current: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify alerts into ``new`` / ``ongoing`` / ``resolved`` by ``alert_key``.

    The ``delivery`` block states plainly that nothing was pushed anywhere.
    Per the task book, writing a local runtime file is not user notification;
    external delivery requires separate explicit authorization.
    """

    previous_map = _as_key_map(previous or [])
    current_map = _as_key_map(current or [])

    new = [alert for key, alert in current_map.items() if key not in previous_map]
    ongoing = [alert for key, alert in current_map.items() if key in previous_map]
    resolved = [
        _recovery_event(alert) for key, alert in previous_map.items() if key not in current_map
    ]

    return {
        "new": new,
        "ongoing": ongoing,
        "resolved": resolved,
        "counts": {
            "new": len(new),
            "ongoing": len(ongoing),
            "resolved": len(resolved),
            "critical": len([a for a in current_map.values() if a.get("severity") == SEVERITY_CRITICAL]),
        },
        "blocks_ai_trader": any(a.get("blocks_ai_trader") for a in current_map.values()),
        "delivery": {
            # Honesty contract: a local write is not a notification.
            "channel": "local_runtime_only",
            "external_push": False,
            "user_notified": False,
            "detail": "仅生成本地运行时告警并在 8000 页面显示；外部推送需用户另行授权。",
        },
    }
