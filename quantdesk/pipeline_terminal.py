"""Map pipeline stage outcomes to charter three-state terminals."""

from __future__ import annotations

import re
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
)
SKIP_REASON_CODES = {
    "data_health_alert_push_not_explicitly_enabled",
    "dry_run",
    "preview_generated",
    "stage_not_selected",
    "planned_offline",
    "skipped_already_completed",
    "skipped_duplicate",
}
SKIP_STATUSES = {
    "skipped", "preview_generated", "dry_run", "planned_offline",
    "skipped_already_completed", "skipped_duplicate",
}


def _stage_skip_reason(stage: Mapping[str, Any]) -> str:
    for code in stage.get("reason_codes") or []:
        text = str(code)
        if text in SKIP_REASON_CODES:
            return text
    payload = stage.get("payload") if isinstance(stage.get("payload"), Mapping) else {}
    stdout = payload.get("stdout_json") if isinstance(payload, Mapping) else {}
    if isinstance(stdout, Mapping):
        reason = str(stdout.get("reason_code") or "")
        if reason in SKIP_REASON_CODES:
            return reason
        if str(stdout.get("status") or "") in SKIP_STATUSES:
            return str(stdout.get("status"))
    if str(stage.get("status") or "") in SKIP_STATUSES:
        return str(stage.get("status"))
    return ""


def _health_push_policy_skip(name: str, stage: Mapping[str, Any]) -> bool:
    """Only the explicit push-policy rejection may override a failed status."""

    if name != "health_alert" or stage.get("status") not in {"failed", "blocked"}:
        return False
    payload = stage.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    stdout = payload.get("stdout_json")
    stdout = stdout if isinstance(stdout, Mapping) else {}
    policy = "data_health_alert_push_not_explicitly_enabled"
    allowed_reasons = {policy, "stage_exit_code:health_alert:4"}
    return (
        stdout.get("status") == "blocked"
        and stdout.get("reason_code") == policy
        and payload.get("exit_code", 4) == 4
        and not payload.get("timeout")
        and all(str(code) in allowed_reasons for code in stage.get("reason_codes") or [])
    )


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
        if _health_push_policy_skip(str(name), stage):
            skipped.append({"stage": str(name), "reason": skip_reason or status})
            continue
        if status in {"completed", "completed_with_warnings", "completed_all_executed"}:
            if skip_reason:
                skipped.append({"stage": str(name), "reason": skip_reason})
            else:
                executed.append(str(name))
            continue
        if status in SKIP_STATUSES:
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


def pipeline_failure_details(report: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Describe recorded failures for saved terminals and read-only UI views.

    This does not change the terminal, infer a cause from an exit code, or
    publish raw exception messages. Exception categories describe the recorded
    type; a processing exception alone does not prove who introduced a bug.
    """

    incoming = report if isinstance(report, Mapping) else {}
    stages = incoming.get("stages")
    stages = stages if isinstance(stages, Mapping) else {}
    details: list[dict[str, str]] = []
    labels = {
        "universe": "股票名册", "market_refresh": "收盘行情", "research_chain": "研究计算",
        "reconcile": "账本核对", "health_alert": "健康通知", "pipeline": "收盘更新",
        "bars_and_valuation": "日线和估值", "benchmarks": "基准指数", "news": "新闻",
        "quarterly": "季度数据",
    }
    failures = {"failed", "blocked", "partial", "error", "timeout", "timed_out", "aborted"}
    family_skips = SKIP_STATUSES | {"skipped_network_false", "skipped_offseason"}

    def add(stage: str, component: str, data: Mapping[str, Any]) -> None:
        raw_type = str(data.get("error_type") or "")
        error_type = raw_type if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,79}", raw_type) else ""
        if error_type in {"TimeoutError", "ReadTimeout", "ConnectTimeout", "ConnectionError", "HTTPError", "URLError"}:
            category, description = "provider_transport", "数据源连接或等待失败"
        elif error_type in {"TypeError", "AttributeError", "NameError", "UnboundLocalError", "KeyError", "IndexError"}:
            category, description = "processing_exception", "程序处理异常，需检查调用或数据结构"
        elif error_type in {"JSONDecodeError", "ParserError", "UnicodeDecodeError"}:
            category, description = "response_format", "返回内容无法解析"
        else:
            category, description = "unclassified", "失败原因尚未分类" if error_type else "未记录异常类型，原因待核"
        label = labels.get(stage, "其他阶段")
        if component:
            label += " / " + labels.get(component, "其他数据项")
        message = f"{label}：{description}" + (f"（{error_type}）" if error_type else "")
        details.append({"stage": stage, "component": component, "error_type": error_type,
                        "category": category, "message": message})

    for name, value in stages.items():
        if not isinstance(value, Mapping):
            continue
        status = str(value.get("status") or "")
        if status in SKIP_STATUSES or _health_push_policy_skip(str(name), value):
            continue
        payload = value.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        stdout = payload.get("stdout_json")
        stdout = stdout if isinstance(stdout, Mapping) else {}
        families = stdout.get("families")
        families = families if isinstance(families, Mapping) else {}
        before = len(details)
        for family, result in families.items():
            if isinstance(result, Mapping) and (
                str(result.get("status") or "") in failures or result.get("ok") is False
            ) and str(result.get("status") or "") not in family_skips:
                add(str(name), str(family), result)
        if len(details) == before and status in failures:
            data = next((d for d in (value, stdout, payload) if d.get("error_type")), value)
            add(str(name), "", data)
    if not stages and str(incoming.get("status") or "") in failures:
        add("pipeline", "", incoming)
    return details
