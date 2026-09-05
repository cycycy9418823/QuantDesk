"""Map pipeline stage outcomes to charter three-state terminals."""

from __future__ import annotations

from typing import Any, Mapping

EXPECTED_STAGES = (
    "universe",
    "market_refresh",
    "research_chain",
    "reconcile",
    "health_alert",
)
SUCCESS_STATUSES = (
    "completed_all_executed",
    "completed_with_skips",
    "completed",
    "completed_with_warnings",
)
SKIP_REASON_CODES = {
    "data_health_alert_push_not_explicitly_enabled",
    "dry_run",
    "preview_generated",
}


def _stage_skip_reason(stage: Mapping[str, Any]) -> str:
    for code in stage.get("reason_codes") or []:
        text = str(code)
        for skip in SKIP_REASON_CODES:
            if skip in text:
                return skip
    payload = stage.get("payload") if isinstance(stage.get("payload"), Mapping) else {}
    stdout = payload.get("stdout_json") if isinstance(payload, Mapping) else {}
    if isinstance(stdout, Mapping):
        reason = str(stdout.get("reason_code") or "")
        if reason in SKIP_REASON_CODES:
            return reason
        if str(stdout.get("status") or "") in {"preview_generated", "skipped"}:
            return str(stdout.get("status"))
    if str(stage.get("status") or "") in {"skipped", "preview_generated"}:
        return str(stage.get("status"))
    return ""


def classify_pipeline_status(stages: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return completed_all_executed / completed_with_skips / failed.

    A health-alert that only failed because push was not switched on is a
    skip, not a package failure.
    """

    skipped: list[dict[str, str]] = []
    failed: list[str] = []
    executed: list[str] = []
    running = False
    incoming = dict(stages or {})
    names = list(dict.fromkeys([*EXPECTED_STAGES, *incoming.keys()]))
    if not incoming:
        return {
            "status": "failed",
            "executed_stages": [],
            "skipped_stages": [],
            "failed_stages": [f"missing:{name}" for name in EXPECTED_STAGES],
        }
    for name in names:
        raw = incoming.get(name)
        if raw is None:
            failed.append(f"missing:{name}")
            continue
        stage = raw if isinstance(raw, Mapping) else {}
        status = str(stage.get("status") or "")
        skip_reason = _stage_skip_reason(stage)
        if status == "running":
            running = True
            continue
        if status in {"completed", "completed_with_warnings", "completed_all_executed"}:
            executed.append(str(name))
            continue
        if skip_reason in SKIP_REASON_CODES or status in {"skipped", "preview_generated"}:
            skipped.append({"stage": str(name), "reason": skip_reason or status})
            continue
        failed.append(str(name))
    if running and not failed:
        terminal = "retry_pending"
    elif failed:
        terminal = "failed"
    elif skipped:
        terminal = "completed_with_skips"
    else:
        terminal = "completed_all_executed"
    return {
        "status": terminal,
        "executed_stages": executed,
        "skipped_stages": skipped,
        "failed_stages": failed,
    }
