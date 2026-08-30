"""Concise local status advisor for QuantDesk.

The status advisor is deliberately read-only. It only inspects local result
files and operation logs, never calls providers, never runs strategies, never
invokes an LLM, and never scans the full A-share market.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from quantdesk.operation_log import load_operation_log
from quantdesk.remediation_advisor import build_remediation_plan
from quantdesk.schema_contracts import build_base_result
from quantdesk.shared import read_json, short_error_text, write_json
from quantdesk.time_utils import now_text
from quantdesk.trading_calendar import resolve_target_complete_trade_date

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATUS_JSON_PATH = PROJECT_ROOT / "results" / "status_latest.json"
STATUS_REPORT_PATH = PROJECT_ROOT / "reports" / "status_report_latest.md"
DISCLAIMER = "仅用于本地状态诊断和流程引导，不构成投资建议。"


INPUT_PATHS: dict[str, str] = {
    "data_update": "results/data_update/data_update_latest.json",
    "data_health": "results/data_health/data_health_dashboard_latest.json",
    "extended_data_gates": "results/data_health/extended_data_gates_latest.json",
    "command_center": "results/command_center_status_latest.json",
    "daily_workflow": "results/daily_workflow_latest.json",
    "market_intelligence": "results/market_intelligence/market_intelligence_latest.json",
    "strategy_validation": "results/strategy_validation_latest.json",
    "consistency_check": "results/paper_vs_backtest_consistency_latest.json",
    "stats_validation": "results/backtest_statistical_validation_latest.json",
    "parameter_sensitivity": "results/parameter_sensitivity_latest.json",
    "observation_report": "results/strategy_observation_context_latest.json",
    "schema_validation": "results/schema_validation_latest.json",
}


def _root(project_root: str | Path | None = None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _project_path(path: str | Path, project_root: str | Path | None = None) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = _root(project_root) / target
    return target


def _load(path: str | Path, project_root: str | Path | None = None) -> dict[str, Any]:
    payload = read_json(_project_path(path, project_root), {})
    return payload if isinstance(payload, dict) else {}


def _status(payload: dict[str, Any]) -> str:
    if not payload:
        return "missing"
    text = str(payload.get("status") or payload.get("workflow_status") or "").strip().lower()
    if text in {"ok", "success", "completed", "passed"}:
        return "passed"
    if text == "partial":
        return "warning"
    if text in {"warning", "failed", "missing", "skipped", "unavailable"}:
        return text
    return "warning" if text else "missing"


def _status_word(status: Any) -> str:
    return {
        "passed": "正常",
        "warning": "提示",
        "failed": "失败",
        "missing": "未完成",
        "skipped": "已跳过",
        "unavailable": "不可用",
    }.get(str(status or "missing"), str(status or "未完成"))


def _int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _iso_date_text(value: Any) -> str:
    text = str(value or "").strip()[:10]
    if not text:
        return ""
    try:
        date.fromisoformat(text)
    except ValueError:
        return ""
    return text


def _current_target_date() -> str:
    try:
        return _iso_date_text(resolve_target_complete_trade_date().get("target_complete_trade_date"))
    except Exception:
        return ""


def _command_with_optional_target(command: str, target_date: str = "") -> str:
    text = " ".join(str(command or "").strip().split())
    if not text:
        return ""
    target = _iso_date_text(target_date) or _current_target_date()
    if target:
        return text.replace("YYYY-MM-DD", target)
    return (
        text.replace(" --target-date YYYY-MM-DD", "")
        .replace("--target-date YYYY-MM-DD ", "")
        .replace("--target-date YYYY-MM-DD", "")
        .strip()
    )


def _freshness_payload(data_health: dict[str, Any]) -> dict[str, Any]:
    for key in ("freshness", "freshness_summary", "data_freshness_summary", "daily_freshness_summary"):
        value = data_health.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _daily_summary(daily: dict[str, Any]) -> dict[str, Any]:
    if not daily:
        return {
            "status": "missing",
            "last_run_at": "",
            "warning_count": 0,
            "failed_count": 0,
            "report_path": "reports/daily_quant_report_latest.md",
            "result_path": "results/daily_workflow_latest.json",
        }
    console = daily.get("daily_console_summary") if isinstance(daily.get("daily_console_summary"), dict) else {}
    guardrails = daily.get("guardrail_summary") if isinstance(daily.get("guardrail_summary"), dict) else {}
    return {
        "status": _status(daily),
        "last_run_at": daily.get("generated_at") or daily.get("checked_at") or "",
        "warning_count": _int(console.get("warning_count"), _int(guardrails.get("warning_count"), len(daily.get("warnings") or []))),
        "failed_count": _int(console.get("failed_count"), len(daily.get("errors") or [])),
        "report_path": console.get("report_path") or daily.get("report_path") or "reports/daily_quant_report_latest.md",
        "result_path": console.get("result_path") or daily.get("latest_path") or "results/daily_workflow_latest.json",
    }


def _is_newer(left: str, right: str) -> bool:
    left = str(left or "")
    right = str(right or "")
    return bool(left and (not right or left > right))


def load_status_inputs(*, project_root: str | Path | None = None) -> dict[str, Any]:
    root = _root(project_root)
    payloads = {name: _load(path, root) for name, path in INPUT_PATHS.items()}
    payloads["operation_log"] = load_operation_log(root / "results" / "operation_log" / "operation_timeline.jsonl", limit=30)
    payloads["generated_at"] = now_text()
    return payloads


def classify_status_problem(inputs: dict[str, Any]) -> dict[str, Any]:
    data_update = inputs.get("data_update") if isinstance(inputs.get("data_update"), dict) else {}
    data_health = inputs.get("data_health") if isinstance(inputs.get("data_health"), dict) else {}
    freshness = _freshness_payload(data_health)
    strategy = inputs.get("strategy_validation") if isinstance(inputs.get("strategy_validation"), dict) else {}
    consistency = inputs.get("consistency_check") if isinstance(inputs.get("consistency_check"), dict) else {}
    observation = inputs.get("observation_report") if isinstance(inputs.get("observation_report"), dict) else {}
    schema = inputs.get("schema_validation") if isinstance(inputs.get("schema_validation"), dict) else {}
    daily = inputs.get("daily_workflow") if isinstance(inputs.get("daily_workflow"), dict) else {}
    market_intel = inputs.get("market_intelligence") if isinstance(inputs.get("market_intelligence"), dict) else {}

    lag = _int(data_update.get("trading_day_lag") or freshness.get("trading_day_lag"))
    stale_count = _int(data_update.get("stale_count") or freshness.get("stale_count"))
    missing_count = _int(data_update.get("missing_count") or freshness.get("missing_count"))
    confirmed_secret_count = _int(schema.get("confirmed_secret_leak_count"))
    secret_reference_count = _int(schema.get("secret_like_reference_count"))

    data_problem = "passed"
    if not data_update and not data_health:
        data_problem = "missing"
    elif lag > 0 or stale_count > 0 or missing_count > 0 or _status(data_update) in {"warning", "failed"}:
        data_problem = "warning"

    validation_statuses = [_status(strategy), _status(consistency)]
    validation_problem = "passed"
    if "failed" in validation_statuses:
        validation_problem = "failed"
    elif "missing" in validation_statuses:
        validation_problem = "missing"
    elif "warning" in validation_statuses:
        validation_problem = "warning"

    review_problem = _status(observation)
    system_problem = "failed" if confirmed_secret_count else ("warning" if secret_reference_count or _status(schema) in {"warning", "missing"} else "passed")
    return {
        "data_problem": data_problem,
        "validation_problem": validation_problem,
        "review_problem": review_problem,
        "system_problem": system_problem,
        "trading_day_lag": lag,
        "stale_count": stale_count,
        "missing_count": missing_count,
        "confirmed_secret_leak_count": confirmed_secret_count,
        "secret_like_reference_count": secret_reference_count,
        "daily_status": _status(daily),
        "daily_warning_count": _daily_summary(daily)["warning_count"],
        "daily_failed_count": _daily_summary(daily)["failed_count"],
        "daily_is_missing": not bool(daily),
        "market_intelligence_status": _status(market_intel),
        "market_intelligence_report_path": market_intel.get("report_path") or "reports/market_intelligence_report_latest.md",
        "data_update_newer_than_daily": _is_newer(
            str(data_update.get("generated_at") or data_update.get("checked_at") or ""),
            str(daily.get("generated_at") or daily.get("checked_at") or ""),
        ),
    }


def _target_from_payload(
    data_update: dict[str, Any],
    data_health: dict[str, Any] | None = None,
    *extra_payloads: dict[str, Any],
) -> str:
    payloads = [payload for payload in (data_update, data_health, *extra_payloads) if isinstance(payload, dict)]
    values: list[Any] = []
    for payload in payloads:
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        freshness = _freshness_payload(payload)
        daily_price = payload.get("daily_price_status") if isinstance(payload.get("daily_price_status"), dict) else {}
        values.extend(
            [
                payload.get("effective_target_date"),
                payload.get("target_complete_trade_date"),
                payload.get("target_date"),
                summary.get("target_complete_trade_date"),
                summary.get("daily_price_target_date"),
                daily_price.get("target_date"),
                freshness.get("target_date"),
                freshness.get("target_complete_trade_date"),
            ]
        )
    iso_dates: list[str] = []
    fallback = ""
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        text = text[:10]
        fallback = fallback or text
        try:
            date.fromisoformat(text)
        except ValueError:
            continue
        iso_dates.append(text)
    if iso_dates:
        return max(iso_dates)
    if fallback:
        text = _iso_date_text(fallback)
        if text:
            return text
    return _current_target_date()


def _is_clean_zip_first_run(project_root: str | Path | None = None) -> bool:
    root = _root(project_root)
    results_dir = root / "results"
    daily_dir = root / "data" / "daily"
    universe_path = root / "data" / "universe" / "low_price_learning_symbols.csv"
    has_daily_files = daily_dir.exists() and any(daily_dir.glob("*.csv"))
    return not results_dir.exists() or (not universe_path.exists() and not has_daily_files)


def _input_has_no_universe(inputs: dict[str, Any]) -> bool:
    data_update = inputs.get("data_update") if isinstance(inputs.get("data_update"), dict) else {}
    data_health = inputs.get("data_health") if isinstance(inputs.get("data_health"), dict) else {}
    daily = inputs.get("daily_workflow") if isinstance(inputs.get("daily_workflow"), dict) else {}
    freshness = _freshness_payload(data_health)
    daily_price = daily.get("daily_price_status") if isinstance(daily.get("daily_price_status"), dict) else {}
    values = [
        data_update.get("analysis_pool_count"),
        data_update.get("selected_count"),
        data_update.get("symbol_count"),
        freshness.get("analysis_pool_count"),
        freshness.get("selected_count"),
        freshness.get("symbol_count"),
        daily_price.get("total_symbols"),
        daily_price.get("symbol_count"),
    ]
    return any(_int(value, default=-1) == 0 for value in values)


def _input_has_universe(inputs: dict[str, Any]) -> bool:
    data_update = inputs.get("data_update") if isinstance(inputs.get("data_update"), dict) else {}
    data_health = inputs.get("data_health") if isinstance(inputs.get("data_health"), dict) else {}
    daily = inputs.get("daily_workflow") if isinstance(inputs.get("daily_workflow"), dict) else {}
    freshness = _freshness_payload(data_health)
    daily_price = daily.get("daily_price_status") if isinstance(daily.get("daily_price_status"), dict) else {}
    values = [
        data_update.get("analysis_pool_count"),
        data_update.get("selected_count"),
        data_update.get("symbol_count"),
        freshness.get("analysis_pool_count"),
        freshness.get("selected_count"),
        freshness.get("symbol_count"),
        daily_price.get("total_symbols"),
        daily_price.get("symbol_count"),
    ]
    return any(_int(value, default=-1) > 0 for value in values)


def _first_from_payloads(payloads: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if value not in (None, "", []):
                return value
    return None


def _data_gate_payloads(data_health: dict[str, Any], extended_data_gates: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    if isinstance(extended_data_gates, dict) and extended_data_gates:
        policy = extended_data_gates.get("operator_action_policy")
        queue = (
            (extended_data_gates.get("gate_chain") or {}).get("queue")
            if isinstance(extended_data_gates.get("gate_chain"), dict)
            else {}
        )
        if isinstance(policy, dict):
            payloads.append(policy)
        if isinstance(queue, dict):
            payloads.append(queue)
        payloads.append(extended_data_gates)
    for key in ("data_health_summary", "key_counts", "extended_data_gates", "extended_data_gate_summary"):
        nested = data_health.get(key)
        if isinstance(nested, dict):
            payloads.append(nested)
    payloads.append(data_health)
    return payloads


def _list_from_payloads(payloads: list[dict[str, Any]], keys: tuple[str, ...]) -> list[str]:
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
    return []


def _download_queue_gate_action(
    data_health: dict[str, Any],
    extended_data_gates: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not data_health and not extended_data_gates:
        return None
    payloads = _data_gate_payloads(data_health, extended_data_gates)
    gate = str(
        _first_from_payloads(
            payloads,
            (
                "extended_data_current_gate",
                "current_gate",
                "extended_import_gate",
                "extended_import_preapply_gate",
                "post_import_gate",
            ),
        )
        or ""
    ).strip()
    gate_next = str(
        _first_from_payloads(
            payloads,
            (
                "extended_data_gate_suggested_next_action",
                "suggested_next_action",
                "extended_data_suggested_next_action",
            ),
        )
        or ""
    ).strip()
    wait_reason = str(
        _first_from_payloads(payloads, ("extended_data_operator_wait_reason", "operator_wait_reason"))
        or ""
    ).strip()
    queue_status = str(
        _first_from_payloads(payloads, ("queue_status", "extended_data_queue_status", "status")) or ""
    ).strip().lower()
    queue_liveness = str(
        _first_from_payloads(payloads, ("queue_liveness", "extended_data_queue_liveness")) or ""
    ).strip().lower()
    queue_progress = str(
        _first_from_payloads(
            payloads,
            ("queue_progress_state", "extended_data_queue_progress_state", "progress_state"),
        )
        or ""
    ).strip()
    stale_gate = (
        wait_reason in {"download_queue_stale", "download_queue_unknown"}
        or queue_liveness == "stale"
    )
    active_gate = (
        wait_reason == "download_queue_active"
        or queue_liveness == "active"
        or (
            gate == "download_queue"
            and queue_status == "running"
            and queue_liveness in {"unknown", ""}
        )
    )
    if not (active_gate or stale_gate):
        return None
    safe_commands = _list_from_payloads(
        payloads,
        ("extended_data_safe_next_commands", "safe_next_commands"),
    )
    command = safe_commands[0] if safe_commands else "python scripts/qd.py tushare-queue-status --summary true"
    blocked_commands = _list_from_payloads(
        payloads,
        ("extended_data_blocked_without_confirmation", "blocked_without_confirmation"),
    )
    reason = (
        "Tushare download queue is stale"
        if stale_gate
        else "Tushare download queue is active"
    )
    if queue_progress:
        reason += f" ({queue_progress})"
    if gate_next:
        reason += f"; gate suggests {gate_next}"
    return {
        "priority": "high",
        "category": "data-gates",
        "severity": "warning",
        "warning_kind": "download_queue_stale" if stale_gate else "download_queue_active",
        "title": "Stale Tushare download queue" if stale_gate else "Active Tushare download queue",
        "reason": reason,
        "why": (
            "Queue metadata still says running, but no recent progress was observed. "
            "Treat it as stale until an operator restarts or retires it."
            if stale_gate
            else "The extended-data queue is still writing staging output, so status must remain read-only."
        ),
        "impact": (
            "Staged imports remain blocked until the stale queue is reviewed."
            if stale_gate
            else "Prevents data updates or staged imports from racing an active download."
        ),
        "command": command,
        "follow_up_command": "python scripts/qd.py data-gates --summary true",
        "network_required": False,
        "safe_next_commands": safe_commands,
        "blocked_without_confirmation": blocked_commands,
        "safety_note": "Read-only monitoring; no token access, network fetch, canonical write, or broker route.",
    }


def _safe_actions_while_download_queue_active(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suppress network/import guidance while staged downloads are still active."""

    safe: list[dict[str, Any]] = []
    for item in actions:
        command = str(item.get("command") or "").strip()
        if not command:
            continue
        if item.get("network_required"):
            continue
        if "data-update" in command:
            continue
        if "import-extended-data" in command and "--apply" in command:
            continue
        safe.append(item)
    return safe


def _prepend_unique_action(action: dict[str, Any] | None, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not action:
        return actions
    command = str(action.get("command") or "")
    merged = [action]
    seen = {command} if command else set()
    for item in actions:
        item_command = str(item.get("command") or "")
        if item_command and item_command in seen:
            continue
        if item_command:
            seen.add(item_command)
        merged.append(item)
    return merged


def build_next_action_recommendations(
    inputs: dict[str, Any],
    problems: dict[str, Any],
    *,
    project_root: str | Path | None = None,
    limit: int = 3,
) -> list[dict[str, str]]:
    data_update = inputs.get("data_update") if isinstance(inputs.get("data_update"), dict) else {}
    data_health = inputs.get("data_health") if isinstance(inputs.get("data_health"), dict) else {}
    extended_data_gates = inputs.get("extended_data_gates") if isinstance(inputs.get("extended_data_gates"), dict) else {}
    daily = inputs.get("daily_workflow") if isinstance(inputs.get("daily_workflow"), dict) else {}
    target = _target_from_payload(data_update, data_health, daily)
    actions: list[dict[str, str]] = []
    gate_action = _download_queue_gate_action(data_health, extended_data_gates)
    if gate_action:
        return [gate_action]
    lag = _int(problems.get("trading_day_lag"))
    stale = _int(problems.get("stale_count"))
    missing = _int(problems.get("missing_count"))
    if _input_has_no_universe(inputs) or (
        _is_clean_zip_first_run(project_root) and not _input_has_universe(inputs)
    ):
        actions.append(
            {
                "category": "data",
                "reason": "clean zip 初次打开没有本地股票池和行情数据；先初始化 demo 股票池，再做 data-health 和 daily。",
                "command": "python scripts/qd.py data-bootstrap --preset low_price_demo --network false",
            }
        )
        actions.append(
            {
                "category": "data",
                "reason": "离线场景可先检查当前本地状态；这只检查，不会补数。",
                "command": "python scripts/qd.py data-health --network false",
            }
        )
    elif lag > 3 or stale > 0 or missing > 0 or problems.get("data_problem") in {"missing", "warning"}:
        actions.append(
            {
                "category": "data",
                "reason": f"本地日线存在 stale/missing，滞后交易日={lag}，过期={stale}，缺失={missing}；联网补齐日线需要显式授权。",
                "command": _command_with_optional_target(
                    "python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network --scope current_analysis_pool",
                    target,
                ),
            }
        )
        actions.append(
            {
                "category": "data",
                "reason": "完成联网补数或离线检查后，用同一目标日期刷新 data-health。",
                "command": _command_with_optional_target(
                    "python scripts/qd.py data-health --target-date YYYY-MM-DD --network false --scope current_analysis_pool",
                    target,
                ),
            }
        )
    elif problems.get("validation_problem") in {"missing", "warning", "failed"}:
        actions.append(
            {
                "category": "validation",
                "reason": "数据状态相对可用，但策略验证或一致性检查缺失/较旧。",
                "command": "python scripts/qd.py daily --network false --with-guardrails true",
            }
        )
    elif problems.get("daily_is_missing") or problems.get("data_update_newer_than_daily") or _status(daily) in {"missing", "warning", "failed"}:
        actions.append(
            {
                "category": "flow",
                "reason": "daily 报告缺失、较旧或有提示；先重新运行每日流程。",
                "command": "python scripts/qd.py daily --network false --with-guardrails true",
            }
        )
    elif problems.get("review_problem") in {"missing", "warning", "failed"}:
        actions.append(
            {
                "category": "review",
                "reason": "学习复盘报告缺失或有提示。",
                "command": _command_with_optional_target(
                    "python scripts/qd.py observation-report --target-date YYYY-MM-DD --network false --llm false --audit true",
                    target,
                ),
            }
        )
    else:
        actions.append(
            {
                "category": "flow",
                "reason": "核心状态没有阻断项，可以打开本地工作台。",
                "command": "python scripts/qd.py app",
            }
        )
    if problems.get("data_update_newer_than_daily"):
        actions.append(
            {
                "category": "flow",
                "reason": "数据已更新，但 daily 报告较旧，请重新运行 daily。",
                "command": "python scripts/qd.py daily --network false --with-guardrails true",
            }
        )
    if problems.get("system_problem") in {"failed", "warning"}:
        actions.append(
            {
                "category": "system",
                "reason": "系统治理状态有提示；确认高风险密钥泄露为 0 后通常无需阻断。",
                "command": "python scripts/qd.py schema-check --results-dir results --network false",
            }
        )
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in actions:
        command = item.get("command", "")
        if command and command not in seen:
            seen.add(command)
            deduped.append(item)
    return deduped[:limit]


def build_quantdesk_status(*, project_root: str | Path | None = None, write: bool = True) -> dict[str, Any]:
    inputs = load_status_inputs(project_root=project_root)
    problems = classify_status_problem(inputs)
    actions = build_next_action_recommendations(inputs, problems, project_root=project_root)
    data_update = inputs.get("data_update") if isinstance(inputs.get("data_update"), dict) else {}
    data_health = inputs.get("data_health") if isinstance(inputs.get("data_health"), dict) else {}
    extended_data_gates = inputs.get("extended_data_gates") if isinstance(inputs.get("extended_data_gates"), dict) else {}
    daily = inputs.get("daily_workflow") if isinstance(inputs.get("daily_workflow"), dict) else {}
    market_intel = inputs.get("market_intelligence") if isinstance(inputs.get("market_intelligence"), dict) else {}
    schema = inputs.get("schema_validation") if isinstance(inputs.get("schema_validation"), dict) else {}
    freshness = _freshness_payload(data_health)
    health_summary = data_health.get("data_health_summary") if isinstance(data_health.get("data_health_summary"), dict) else {}
    data_scope = data_health.get("data_scope") or freshness.get("data_scope") or data_update.get("data_scope") or "unknown"
    symbol_count = _int(
        data_health.get("symbol_count")
        or freshness.get("symbol_count")
        or len(data_health.get("symbols_checked") or [])
        or data_update.get("symbol_count")
        or len(data_update.get("symbols_checked") or [])
    )
    coverage_date = (
        freshness.get("coverage_date")
        or health_summary.get("coverage_date")
        or data_health.get("coverage_date")
        or data_update.get("coverage_date")
        or data_update.get("latest_local_daily_date")
        or ""
    )
    latest_date_max = (
        freshness.get("latest_date_max")
        or health_summary.get("latest_date_max")
        or data_health.get("latest_date_max")
        or data_update.get("latest_date_max")
        or data_update.get("latest_local_daily_max_date")
        or ""
    )
    remediation_plan = build_remediation_plan(
        data_update=data_update,
        data_health=data_health,
        daily_payload=daily,
        schema_payload=schema,
        market_intel_payload=market_intel,
        project_root=project_root,
    )
    remediation_actions = remediation_plan.get("actions") if isinstance(remediation_plan.get("actions"), list) else []
    gate_action = _download_queue_gate_action(data_health, extended_data_gates)
    if gate_action:
        actions = _prepend_unique_action(gate_action, _safe_actions_while_download_queue_active(actions))
        remediation_actions = _prepend_unique_action(
            gate_action,
            _safe_actions_while_download_queue_active(remediation_actions),
        )
        remediation_plan = dict(remediation_plan)
        remediation_plan["actions"] = remediation_actions
        remediation_plan["suggested_next_action"] = gate_action.get("command")
    target = _target_from_payload(data_update, data_health, daily, market_intel)
    overall = "passed"
    if "failed" in {problems["data_problem"], problems["validation_problem"], problems["review_problem"], problems["system_problem"]}:
        overall = "failed"
    elif remediation_actions or any(problems[key] in {"warning", "missing"} for key in ("data_problem", "validation_problem", "review_problem", "system_problem")):
        overall = "warning"
    payload = build_base_result(
        status=overall,
        ok=True,
        target_date=target,
        warnings=[] if overall == "passed" else ["本地状态存在提示，请按 remediation_actions 逐步处理。"],
        suggested_next_action=(remediation_actions[0].get("command") if remediation_actions else (actions[0]["command"] if actions else "python scripts/qd.py app")),
        network=False,
        llm_enabled=False,
        full_scan=False,
        data_summary={
            "status": problems["data_problem"],
            "data_scope": data_scope,
            "symbol_count": symbol_count,
            "coverage_date": coverage_date,
            "latest_date_max": latest_date_max,
            "trading_day_lag": problems["trading_day_lag"],
            "stale_count": problems["stale_count"],
            "missing_count": problems["missing_count"],
        },
        validation_summary={"status": problems["validation_problem"]},
        daily_summary=_daily_summary(daily),
        review_summary={"status": problems["review_problem"]},
        system_summary={
            "status": problems["system_problem"],
            "schema_status": _status(schema),
            "confirmed_secret_leak_count": problems["confirmed_secret_leak_count"],
            "secret_like_reference_count": problems["secret_like_reference_count"],
        },
        next_actions=actions,
        remediation_plan=remediation_plan,
        remediation_actions=remediation_actions[:5],
        input_files={name: path for name, path in INPUT_PATHS.items()},
        disclaimer=DISCLAIMER,
    )
    if write:
        write_status_latest(payload)
    return payload


def format_status_console_output(payload: dict[str, Any]) -> str:
    data = payload.get("data_summary") if isinstance(payload.get("data_summary"), dict) else {}
    validation = payload.get("validation_summary") if isinstance(payload.get("validation_summary"), dict) else {}
    daily = payload.get("daily_summary") if isinstance(payload.get("daily_summary"), dict) else {}
    system = payload.get("system_summary") if isinstance(payload.get("system_summary"), dict) else {}
    actions = payload.get("remediation_actions") if isinstance(payload.get("remediation_actions"), list) else []
    if not actions:
        actions = payload.get("next_actions") if isinstance(payload.get("next_actions"), list) else []
    lines = [
        "QuantDesk 当前状态",
        "",
        f"数据：{_status_word(data.get('status'))}｜全池覆盖到 {data.get('coverage_date') or '-'}，目标滞后 {data.get('trading_day_lag', 0)} 个交易日",
        f"每日流程：{_status_word(daily.get('status'))}｜warning={daily.get('warning_count', 0)}，报告 {daily.get('report_path') or '-'}",
        f"验证：{_status_word(validation.get('status'))}｜strategy-validate / consistency-check 汇总",
        f"系统：{_status_word(system.get('status'))}｜高风险密钥泄露 {system.get('confirmed_secret_leak_count', 0)}，低风险敏感词引用 {system.get('secret_like_reference_count', 0)}",
        "",
        "优先修复：",
    ]
    for index, action in enumerate(actions[:3], start=1):
        reason = action.get("why") or action.get("reason") or action.get("title") or "-"
        network_note = "需要显式联网授权" if action.get("network_required") else "离线可运行，不会联网"
        lines.append(f"{index}. {action.get('command')}")
        lines.append(f"   {network_note}；原因：{reason}")
    lines.extend(
        [
            "",
            "status 默认只读：不联网、不调用 LLM、不执行策略、不真实下单。",
        ]
    )
    return "\n".join(lines[:15])


def write_status_latest(payload: dict[str, Any], json_path: str | Path = STATUS_JSON_PATH, report_path: str | Path = STATUS_REPORT_PATH) -> dict[str, str]:
    json_target = _project_path(json_path)
    report_target = _project_path(report_path)
    write_json(json_target, payload)
    report_target.parent.mkdir(parents=True, exist_ok=True)
    report_target.write_text(build_status_report(payload), encoding="utf-8")
    return {"json_path": str(json_target), "report_path": str(report_target)}


def build_status_report(payload: dict[str, Any]) -> str:
    lines = [
        "# QuantDesk 当前状态",
        "",
        DISCLAIMER,
        "",
        "```text",
        format_status_console_output(payload),
        "```",
        "",
        "## 优先修复",
    ]
    for action in payload.get("remediation_actions") or payload.get("next_actions") or []:
        if not isinstance(action, dict):
            continue
        reason = action.get("why") or action.get("reason") or action.get("title") or ""
        lines.append(f"- {short_error_text(reason)}: `{action.get('command')}`")
    return "\n".join(lines) + "\n"
