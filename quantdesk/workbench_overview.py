"""Read-only workbench overview payloads for the QuantDesk UI.

This module only reads local files that already exist.  It never calls
providers, never invokes an LLM, never starts a workflow, and never scans the
full A-share market.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.daily_price_update_engine import infer_target_date
from quantdesk.schema_contracts import build_base_result
from quantdesk.shared import read_json, short_error_text
from quantdesk.symbol_display_utils import summarize_symbols

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISCLAIMER = "仅用于本地学习研究和工作流状态整理，不构成投资建议。"
SAFETY_NOTE = "以下建议仅为数据、流程和验证顺序提示，不是买卖建议。"
COMMAND_FIELD_KEYS = {"command", "suggested_next_action", "follow_up_command", "remediation_command"}


def _root(project_root: str | Path | None = None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _project_path(path: str | Path, project_root: str | Path | None = None) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = _root(project_root) / target
    return target


def _rel(path: str | Path, project_root: str | Path | None = None) -> str:
    target = _project_path(path, project_root)
    try:
        return str(target.relative_to(_root(project_root))).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _load_json(path: str | Path, project_root: str | Path | None = None) -> dict[str, Any]:
    payload = read_json(_project_path(path, project_root), default={})
    return payload if isinstance(payload, dict) else {}


def _mtime_text(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _status(value: Any, *, exists: bool = True) -> str:
    if not exists:
        return "missing"
    text = str(value or "").strip().lower()
    if text in {"passed", "warning", "failed", "skipped", "missing", "partial", "unavailable"}:
        return text
    if text in {"ok", "success"}:
        return "passed"
    if text in {"error", "exception"}:
        return "failed"
    return "warning" if not text else text


def _status_rank(status: str) -> int:
    return {
        "passed": 0,
        "skipped": 1,
        "missing": 2,
        "unavailable": 2,
        "partial": 3,
        "warning": 4,
        "failed": 5,
    }.get(str(status or "missing"), 4)


def _worst_status(statuses: list[str]) -> str:
    if not statuses:
        return "missing"
    return max(statuses, key=_status_rank)


def _warnings(payload: dict[str, Any]) -> list[str]:
    value = payload.get("warnings") or []
    if isinstance(value, list):
        return [short_error_text(item, max_len=180) for item in value if str(item or "").strip()]
    if value:
        return [short_error_text(value, max_len=180)]
    return []


def _first_path(paths: Any) -> str:
    if isinstance(paths, list) and paths:
        return str(paths[0])
    if isinstance(paths, str):
        return paths
    return ""


def _nested_dict(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _metric_value(value: Any, default: Any = "missing") -> Any:
    if value is None or value == "":
        return default
    return value


def _result_time(summary: dict[str, Any]) -> datetime | None:
    text = str(summary.get("last_run_at") or "")
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _current_complete_trade_date() -> str:
    try:
        target, _note = infer_target_date(None)
        return target
    except Exception:
        return ""


def _target_from_payload(payload: dict[str, Any] | None = None, *, fallback: str = "", use_current: bool = True) -> str:
    payload = payload if isinstance(payload, dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    freshness = _nested_dict(payload, "freshness_summary", "freshness", "data_freshness_summary", "daily_freshness_summary")
    daily_price = payload.get("daily_price_status") if isinstance(payload.get("daily_price_status"), dict) else {}
    for value in (
        payload.get("target_date"),
        payload.get("effective_target_date"),
        payload.get("target_complete_trade_date"),
        summary.get("target_complete_trade_date"),
        summary.get("daily_price_target_date"),
        freshness.get("target_date"),
        freshness.get("target_complete_trade_date"),
        daily_price.get("target_date"),
        fallback,
    ):
        text = str(value or "").strip()
        if text and text not in {"YYYY-MM-DD", "-"}:
            return text[:10]
    return _current_complete_trade_date() if use_current else ""


def _target_from_path(path: str | Path) -> str:
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", str(path))
    return match.group(1) if match else ""


def _fallback_target(*payloads: dict[str, Any]) -> str:
    for payload in payloads:
        target = _target_from_payload(payload, use_current=False)
        if target:
            return target
    return _current_complete_trade_date()


def _command_with_target(command: str, target_date: str = "") -> str:
    text = str(command or "").strip()
    if "YYYY-MM-DD" not in text:
        return text
    target = str(target_date or "").strip() or _current_complete_trade_date()
    if target:
        return text.replace("YYYY-MM-DD", target[:10])
    return text.replace(" --target-date YYYY-MM-DD", "").replace("--target-date YYYY-MM-DD ", "")


def _text_with_target_tokens(text: str, target_date: str = "") -> str:
    rendered = _command_with_target(text, target_date)
    if "{target_date}" not in rendered:
        return rendered
    target = str(target_date or "").strip() or _current_complete_trade_date()
    if target:
        return rendered.replace("{target_date}", target[:10])
    return rendered


def _sanitize_command_fields(value: Any, target_date: str = "") -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key in COMMAND_FIELD_KEYS and isinstance(item, str):
                sanitized[key] = _text_with_target_tokens(item, target_date)
            else:
                sanitized[key] = _sanitize_command_fields(item, target_date)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_command_fields(item, target_date) for item in value]
    return value


def _spec_command(name: str, target_date: str = "") -> str:
    return _command_with_target(RESULT_SPECS[name]["command"], target_date)


def _scope_symbols(payload: dict[str, Any]) -> list[str]:
    for key in ("symbols_checked", "selected_symbols", "symbols", "processed_symbols"):
        value = payload.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item or "").strip()]
    return []


def _scope_display_fields(payload: dict[str, Any]) -> dict[str, Any]:
    symbols = _scope_symbols(payload)
    summary = summarize_symbols(symbols, max_visible=5)
    analysis_pool_count = int(
        payload.get("analysis_pool_count")
        or payload.get("requested_count")
        or payload.get("included_count")
        or payload.get("symbol_count")
        or len(symbols)
        or 0
    )
    selected_count = int(payload.get("selected_count") or payload.get("symbol_count") or len(symbols) or 0)
    max_symbols = int(payload.get("max_symbols") or 0)
    truncated = bool(payload.get("truncated") or (analysis_pool_count and selected_count and selected_count < analysis_pool_count))
    truncation_reason = str(payload.get("truncation_reason") or "")
    if not truncation_reason and truncated and max_symbols:
        truncation_reason = f"由于 max-symbols={max_symbols}，本次只检查前 {selected_count} 只。"
    return {
        "scope_label": payload.get("scope_label") or payload.get("data_scope") or "unknown",
        "scope_source": payload.get("scope_source") or "",
        "requested_count": payload.get("requested_count") or analysis_pool_count,
        "analysis_pool_count": analysis_pool_count,
        "selected_count": selected_count,
        "max_symbols": max_symbols,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "symbols_preview": payload.get("symbols_preview") or summary.get("visible") or [],
        "symbols_omitted_count": payload.get("symbols_omitted_count") or max(0, selected_count - len(payload.get("symbols_preview") or summary.get("visible") or [])),
    }


def validate_workbench_state_consistency(data_update_raw: dict[str, Any], data_health_raw: dict[str, Any]) -> dict[str, Any]:
    """Compare local status files without merging their metrics."""

    warnings: list[str] = []
    update_target = str(
        data_update_raw.get("effective_target_date")
        or data_update_raw.get("target_complete_trade_date")
        or data_update_raw.get("target_date")
        or ""
    )
    health_target = str(data_health_raw.get("target_date") or data_health_raw.get("target_complete_trade_date") or "")
    health_freshness = _nested_dict(data_health_raw, "freshness_summary", "freshness", "data_freshness_summary", "daily_freshness_summary")
    update_scope = str(data_update_raw.get("data_scope") or data_update_raw.get("symbol_source") or "unknown")
    health_scope = str(health_freshness.get("data_scope") or data_health_raw.get("data_scope") or health_freshness.get("symbol_source") or "unknown")
    update_symbols = set(_scope_symbols(data_update_raw))
    health_symbols = set(_scope_symbols(health_freshness) or _scope_symbols(data_health_raw))
    if update_target and health_target and update_target != health_target:
        warnings.append("data-update 和 data-health 目标日期不同，请重新运行 data-health。")
    if update_scope and health_scope and update_scope != "unknown" and health_scope != "unknown" and update_scope != health_scope:
        warnings.append("两者统计范围不同。")
    if update_symbols and health_symbols and not update_symbols.issubset(health_symbols):
        warnings.append("data-update 检查股票不是 data-health 股票集合的子集。")
    status = "warning" if warnings else "passed"
    return {
        "status": status,
        "warnings": warnings,
        "update_target_date": update_target,
        "health_target_date": health_target,
        "update_data_scope": update_scope,
        "health_data_scope": health_scope,
        "suggested_next_action": "当前 data-update / data-health / market-intel 使用的股票范围不同。请用同一 scope 重新运行。" if warnings else "",
        "remediation_commands": [
            "python scripts/qd.py data-health --network false --scope current_analysis_pool",
            "python scripts/qd.py market-intel --network false --scope current_analysis_pool",
        ] if warnings else [],
    }


RESULT_SPECS: dict[str, dict[str, str]] = {
    "status": {
        "path": "results/status_latest.json",
        "command": "python scripts/qd.py status",
        "title": "QuantDesk 状态",
    },
    "daily": {
        "path": "results/daily_workflow_latest.json",
        "command": "python scripts/qd.py daily --network false --with-guardrails true",
        "title": "每日流程",
    },
    "market-intel": {
        "path": "results/market_intelligence/market_intelligence_latest.json",
        "command": "python scripts/qd.py market-intel --target-date YYYY-MM-DD --price-limit 20 --network false --scope current_analysis_pool",
        "title": "市场观察",
    },
    "mainline-radar": {
        "path": "results/market_intelligence/mainline_radar_latest.json",
        "command": "python scripts/qd.py mainline-radar --network false",
        "title": "Market Mainline Radar",
    },
    "data-readiness": {
        "path": "results/data_health/extended_data_readiness_latest.json",
        "command": "python scripts/qd.py data-readiness --summary true",
        "title": "Extended Data Readiness",
    },
    "data-gates": {
        "path": "results/data_health/extended_data_gates_latest.json",
        "command": "python scripts/qd.py data-gates --summary true",
        "title": "Extended Data Gates",
    },
    "tushare-queue-status": {
        "path": "results/data_health/tushare_queue_status_latest.json",
        "command": "python scripts/qd.py tushare-queue-status --summary true",
        "title": "Tushare Queue Status",
    },
    "tushare-minute-status": {
        "path": "results/data_health/tushare_minute_status_latest.json",
        "command": "python scripts/qd.py tushare-minute-status --summary true",
        "title": "Tushare Minute Status",
    },
    "tushare-minute-coverage": {
        "path": "results/data_health/tushare_minute_coverage_latest.json",
        "command": "python scripts/qd.py tushare-minute-coverage --summary true",
        "title": "Tushare Minute Coverage",
    },
    "tushare-retry-plan": {
        "path": "results/data_health/tushare_retry_plan_latest.json",
        "command": "python scripts/qd.py tushare-retry-plan --summary true",
        "title": "Tushare Retry Plan",
    },
    "audit-extended-import": {
        "path": "results/data_health/extended_staged_import_audit_latest.json",
        "command": "python scripts/qd.py audit-extended-import --summary true --max-files-per-rule 0",
        "title": "Extended Staged Import Audit",
    },
    "verify-extended-import": {
        "path": "results/data_health/extended_import_verification_latest.json",
        "command": "python scripts/qd.py verify-extended-import --summary true",
        "title": "Extended Import Verification",
    },
    "data-update": {
        "path": "results/data_update/data_update_latest.json",
        "command": "python scripts/qd.py data-update --target-date YYYY-MM-DD --network false --scope current_analysis_pool",
        "title": "数据更新离线检查",
    },
    "data-health": {
        "path": "results/data_health/data_health_dashboard_latest.json",
        "fallback_path": "results/data_health_dashboard_latest.json",
        "command": "python scripts/qd.py data-health --target-date YYYY-MM-DD --network false --scope current_analysis_pool",
        "title": "数据健康",
    },
    "strategy-validate": {
        "path": "results/strategy_validation_latest.json",
        "command": "python scripts/qd.py strategy-validate --symbols 600744,002115 --target-date YYYY-MM-DD --network false",
        "title": "策略验证",
    },
    "consistency-check": {
        "path": "results/paper_vs_backtest_consistency_latest.json",
        "command": "python scripts/qd.py consistency-check --symbol 600744 --target-date YYYY-MM-DD --network false",
        "title": "一致性检查",
    },
    "feature-audit": {
        "path": "results/feature_quality_audit_latest.json",
        "command": "python scripts/qd.py feature-audit --symbols 600744,002115 --target-date YYYY-MM-DD --network false",
        "title": "特征审计",
    },
    "stats-validate": {
        "path": "results/backtest_statistical_validation_latest.json",
        "command": "python scripts/qd.py stats-validate --symbol 600744 --target-date YYYY-MM-DD --network false --iterations 20 --seed 42",
        "title": "统计验证",
    },
    "param-sensitivity": {
        "path": "results/parameter_sensitivity_latest.json",
        "command": "python scripts/qd.py param-sensitivity --symbol 600744 --target-date YYYY-MM-DD --network false --max-combinations 20 --seed 42",
        "title": "参数敏感性",
    },
    "observation-report": {
        "path": "results/strategy_observation_context_latest.json",
        "command": "python scripts/qd.py observation-report --symbol 600744 --target-date YYYY-MM-DD --network false --llm false --audit true",
        "title": "学习复盘",
    },
    "agent-report-audit": {
        "path": "results/strategy_observation_report_audit_latest.json",
        "command": "python scripts/qd.py agent-audit",
        "title": "Agent 审计",
    },
    "schema-check": {
        "path": "results/schema_validation_latest.json",
        "command": "python scripts/qd.py schema-check --results-dir results --network false",
        "title": "Schema 检查",
    },
    "command-center": {
        "path": "results/command_center_status_latest.json",
        "command": "python scripts/qd.py command-center --summary true",
        "title": "命令中心",
    },
}


def _result_summary(name: str, project_root: str | Path | None = None) -> dict[str, Any]:
    spec = RESULT_SPECS[name]
    path = _project_path(spec["path"], project_root)
    if not path.exists() and spec.get("fallback_path"):
        path = _project_path(spec["fallback_path"], project_root)
    payload = _load_json(path, project_root)
    exists = path.exists() and path.is_file()
    target_date = _target_from_payload(payload)
    payload_for_display = _sanitize_command_fields(payload, target_date)
    status = _status(payload_for_display.get("status"), exists=exists)
    command = _spec_command(name, target_date)
    return {
        "name": name,
        "title": spec["title"],
        "status": status,
        "exists": exists,
        "last_run_at": payload_for_display.get("generated_at") or payload_for_display.get("checked_at") or _mtime_text(path),
        "output_paths": [str(payload_for_display.get("output_path") or _rel(path, project_root))] if exists else [],
        "warnings": _warnings(payload_for_display),
        "failed_reason": short_error_text(payload_for_display.get("failed_reason") or ""),
        "suggested_next_action": _text_with_target_tokens(str(payload_for_display.get("suggested_next_action") or command), target_date),
        "command": command,
        "raw": payload_for_display,
    }


def _command_lookup(project_root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    payload = _load_json("results/command_center_status_latest.json", project_root)
    rows = payload.get("commands") if isinstance(payload.get("commands"), list) else []
    target_date = _target_from_payload(payload)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("command"):
            row_target = _target_from_payload(row, fallback=target_date)
            out[str(row["command"])] = _sanitize_command_fields(row, row_target)
    return out


def _merge_command_status(summary: dict[str, Any], commands: dict[str, dict[str, Any]]) -> dict[str, Any]:
    row = commands.get(summary["name"]) or {}
    if not row:
        return summary
    if row.get("last_status") and summary["status"] == "missing":
        summary["status"] = _status(row.get("last_status"))
    summary["last_run_at"] = summary.get("last_run_at") or row.get("last_run_at") or ""
    summary["output_paths"] = summary.get("output_paths") or row.get("output_paths") or []
    summary["warnings"] = summary.get("warnings") or row.get("warnings") or []
    summary["failed_reason"] = summary.get("failed_reason") or row.get("failed_reason") or ""
    summary["suggested_next_action"] = summary.get("suggested_next_action") or row.get("suggested_next_action") or summary.get("command")
    return summary


def build_workbench_next_actions(
    *,
    project_root: str | Path | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    root = _root(project_root)
    actions: list[dict[str, Any]] = []
    current_target = _current_complete_trade_date()
    data_daily = root / "data" / "daily"
    data_intraday = root / "data" / "intraday"
    if not data_daily.exists():
        actions.append(
            {
                "priority": 2,
                "category": "data",
                "message": "尚未发现本地日线数据。可以先运行 daily-retain，或导入 data/daily/{symbol}.csv。",
                "command": _command_with_target("python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network --scope current_analysis_pool", current_target),
                "reason": "缺少 data/daily 会影响数据健康、策略验证和复盘可信度；联网补齐日线需要显式授权。",
                "safety_note": SAFETY_NOTE,
            }
        )
    data_update = _result_summary("data-update", root)
    update_raw = data_update.get("raw") if isinstance(data_update.get("raw"), dict) else {}
    lag = int(update_raw.get("trading_day_lag") or 0) if update_raw else 0
    latest_update_target = str(
        update_raw.get("effective_target_date") or update_raw.get("target_complete_trade_date") or update_raw.get("target_date") or ""
    )
    action_target = current_target or latest_update_target
    if data_update["status"] != "missing" and current_target and latest_update_target and latest_update_target != current_target:
        actions.append(
            {
                "priority": 2,
                "category": "data",
                "message": f"当前理论完整交易日已变为 {current_target}，最近数据更新检查仍停留在 {latest_update_target}。",
                "command": f"python scripts/qd.py data-update --target-date {current_target} --network false --scope current_analysis_pool",
                "reason": "离线检查 / 刷新本地状态；这只检查，不会补数。若确认需要补齐日线，请显式运行 --enable-network。",
                "safety_note": SAFETY_NOTE,
            }
        )
    if lag > 0:
        actions.append(
            {
                "priority": 1,
                "category": "data",
                "message": f"本地日线已落后 {lag} 个交易日，请进入数据更新工作台。",
                "command": _command_with_target("python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network --scope current_analysis_pool", action_target),
                "reason": "联网补齐日线；需要显式联网授权。离线 data-update 只检查，不会补数。",
                "safety_note": SAFETY_NOTE,
            }
        )
    if int(update_raw.get("fresh_count") or 0) > 0 and int(update_raw.get("stale_count") or 0) > 0:
        actions.append(
            {
                "priority": 2,
                "category": "data",
                "message": "部分股票已更新，但当前统计范围仍未整体覆盖到目标日期。",
                "command": _spec_command("data-health", action_target),
                "reason": "如果只更新了 600744 / 002115，低价学习池其他股票仍可能停在较早日期；请用同一 target-date 和同一统计范围重新运行 data-health。",
                "safety_note": SAFETY_NOTE,
            }
        )
    if not data_daily.exists():
        actions.append(
            {
                "priority": 2,
                "category": "data",
                "message": "补齐或导入日线后，再运行 daily-retain 保存每日留存 manifest。",
                "command": _command_with_target("python scripts/qd.py daily-retain --symbols 600744,002115 --target-date YYYY-MM-DD --network false", action_target),
                "reason": "daily-retain 记录版本、输入、输出和 hash；它不是联网补数入口。",
                "safety_note": SAFETY_NOTE,
            }
        )
    data_health = _result_summary("data-health", root)
    if data_health["status"] == "missing":
        actions.append(
            {
                "priority": 2,
                "category": "data",
                "message": "尚未运行 data-health，建议先查看本地日线新鲜度、分钟线覆盖率和 provider 状态。",
                "command": _spec_command("data-health", action_target),
                "reason": "data-health 是今日流程的第一层数据可观测入口。",
                "safety_note": SAFETY_NOTE,
            }
        )
    health_raw = data_health.get("raw") or {}
    market_intel = _result_summary("market-intel", root)
    market_raw = market_intel.get("raw") if isinstance(market_intel.get("raw"), dict) else {}
    scope_set = {
        str(update_raw.get("data_scope") or "unknown"),
        str((_nested_dict(health_raw, "freshness") or health_raw).get("data_scope") or "unknown"),
        str(market_raw.get("data_scope") or market_raw.get("scope") or "unknown"),
    }
    scope_set.discard("unknown")
    if len(scope_set) > 1:
        actions.append(
            {
                "priority": 1,
                "category": "data",
                "message": "当前 data-update / data-health / market-intel 使用的股票范围不同。",
                "command": "python scripts/qd.py data-health --network false --scope current_analysis_pool",
                "reason": "先用统一 scope 刷新数据健康，再用同一 scope 重新生成市场观察。",
                "follow_up_command": "python scripts/qd.py market-intel --network false --scope current_analysis_pool",
                "safety_note": SAFETY_NOTE,
            }
        )
    intraday = _nested_dict(health_raw, "intraday_coverage_summary", "intraday", "intraday_summary")
    intraday_target = action_target or _current_complete_trade_date()
    if not data_intraday.exists() or float(intraday.get("coverage_rate") or 0) <= 0:
        actions.append(
            {
                "priority": 3,
                "category": "data",
                "message": "日线状态不等于分钟线状态；分钟线缺失只影响盘中触发验证。",
                "command": f"导入 data/intraday/{{period}}/{intraday_target}/{{symbol}}.csv 后重新运行 data-health",
                "reason": "缺少分钟线时不能用日线冒充盘中触发验证。",
                "safety_note": SAFETY_NOTE,
            }
        )
    strategy = _result_summary("strategy-validate", root)
    if strategy["status"] == "missing":
        actions.append(
            {
                "priority": 4,
                "category": "validation",
                "message": "strategy-validate 尚未运行，建议先完成策略验证再进入复盘报告。",
                "command": _spec_command("strategy-validate", action_target),
                "reason": "策略验证用于检查未来函数和执行假设。",
                "safety_note": SAFETY_NOTE,
            }
        )
    consistency = _result_summary("consistency-check", root)
    observation = _result_summary("observation-report", root)
    update_time = _result_time(data_update)
    stale_validation_commands = []
    for summary_name, command_name in (
        ("strategy-validate", "strategy-validate"),
        ("consistency-check", "consistency-check"),
        ("observation-report", "observation-report"),
    ):
        summary = strategy if summary_name == "strategy-validate" else (consistency if summary_name == "consistency-check" else observation)
        summary_time = _result_time(summary)
        if update_time and (summary_time is None or summary_time < update_time):
            stale_validation_commands.append(command_name)
    if update_time and stale_validation_commands:
        actions.append(
            {
                "priority": 4,
                "category": "validation",
                "message": "数据更新状态比部分验证结果更新，建议重新运行验证和复盘流程。",
                "command": _spec_command("strategy-validate", action_target),
                "reason": "建议依次刷新 strategy-validate、consistency-check 和 observation-report，避免用旧验证结果解释新数据状态。",
                "safety_note": SAFETY_NOTE,
                "stale_items": stale_validation_commands,
            }
        )
    if consistency["status"] in {"warning", "failed"}:
        actions.append(
            {
                "priority": 5,
                "category": "validation",
                "message": "已有 consistency-check warning，请先查看一致性报告。",
                "command": _spec_command("consistency-check", action_target),
                "reason": consistency.get("failed_reason") or "纸面组合和回测口径存在需要解释的差异。",
                "safety_note": SAFETY_NOTE,
            }
        )
    if observation["status"] == "missing" and strategy["status"] in {"passed", "warning"}:
        actions.append(
            {
                "priority": 6,
                "category": "review",
                "message": "可以生成 observation-report 做学习复盘。",
                "command": _spec_command("observation-report", action_target),
                "reason": "复盘报告会整理已有本地证据，不调用 LLM，除非你显式启用。",
                "safety_note": SAFETY_NOTE,
            }
        )
    selected = sorted(actions, key=lambda row: int(row.get("priority") or 99))[: max(0, int(limit))]
    status = "warning" if selected else "passed"
    return build_base_result(
        status=status,
        ok=True,
        warnings=[] if selected else [],
        suggested_next_action=selected[0]["command"] if selected else "当前可从今日工作台继续查看已有结果。",
        actions=selected,
        action_count=len(selected),
        network=False,
        llm_enabled=False,
        disclaimer=DISCLAIMER,
    )


def build_workbench_overview(*, project_root: str | Path | None = None) -> dict[str, Any]:
    root = _root(project_root)
    commands = _command_lookup(root)
    summaries = {name: _merge_command_status(_result_summary(name, root), commands) for name in RESULT_SPECS}
    data_health = summaries["data-health"]
    health_raw = data_health.get("raw") if isinstance(data_health.get("raw"), dict) else {}
    freshness = _nested_dict(health_raw, "freshness_summary", "freshness", "data_freshness_summary", "daily_freshness_summary")
    intraday = _nested_dict(health_raw, "intraday_coverage_summary", "intraday", "intraday_summary")
    provider_stats = _nested_dict(health_raw, "provider_failure_stats", "providers", "provider_summary")
    grade = _nested_dict(health_raw, "data_grade_trend", "grade", "grade_trend")
    data_update = summaries["data-update"]
    update_raw = data_update.get("raw") if isinstance(data_update.get("raw"), dict) else {}
    state_consistency = validate_workbench_state_consistency(update_raw, health_raw)
    market_raw = summaries["market-intel"].get("raw") if isinstance(summaries["market-intel"].get("raw"), dict) else {}
    overview_target = _fallback_target(market_raw, health_raw, update_raw)
    scope_values = {
        "data-update": str(update_raw.get("data_scope") or "unknown"),
        "data-health": str((freshness or health_raw).get("data_scope") or health_raw.get("data_scope") or "unknown"),
        "market-intel": str(market_raw.get("data_scope") or market_raw.get("scope") or "unknown"),
    }
    known_scopes = {value for value in scope_values.values() if value and value != "unknown"}
    if len(known_scopes) > 1:
        state_consistency.setdefault("warnings", []).append("当前 data-update / data-health / market-intel 使用的股票范围不同。")
        state_consistency["status"] = "warning"
        state_consistency["market_intel_data_scope"] = scope_values["market-intel"]
        state_consistency["remediation_commands"] = [
            "python scripts/qd.py data-health --network false --scope current_analysis_pool",
            "python scripts/qd.py market-intel --network false --scope current_analysis_pool",
        ]
    update_notes: list[str] = []
    if int(update_raw.get("fresh_count") or 0) > 0 and int(update_raw.get("stale_count") or 0) > 0:
        update_notes.append(
            f"部分股票已更新到目标日期，但仍有 {int(update_raw.get('stale_count') or 0)} 只股票停留在较早日期；首页显示的是全池覆盖状态，不代表已更新股票回退。"
        )
    update_notes.extend(state_consistency.get("warnings") or [])
    status_cards = [
        {
            "id": "data_update",
            "title": "数据更新",
            "status": data_update["status"],
            "last_run_at": data_update["last_run_at"],
            "metrics": {
                "data_scope": _metric_value(update_raw.get("data_scope")),
                "target_complete_trade_date": _metric_value(update_raw.get("target_complete_trade_date")),
                "coverage_date": _metric_value(update_raw.get("coverage_date") or update_raw.get("latest_local_daily_date")),
                "latest_date_max": _metric_value(update_raw.get("latest_date_max") or update_raw.get("latest_local_daily_max_date")),
                "fresh_count": _metric_value(update_raw.get("fresh_count"), 0),
                "stale_count": _metric_value(update_raw.get("stale_count"), 0),
                "missing_count": _metric_value(update_raw.get("missing_count"), 0),
            },
            "symbol_lists": {
                "updated": update_raw.get("updated_symbols") or update_raw.get("latest_updated_symbols") or [],
                "stale": update_raw.get("stale_symbols") or [],
                "missing": update_raw.get("missing_symbols") or [],
                "scope_preview": update_raw.get("symbols_preview") or [],
            },
            "scope_summary": _scope_display_fields(update_raw),
            "notes": update_notes,
            "result_path": _first_path(data_update.get("output_paths")),
            "suggested_next_action": data_update.get("suggested_next_action"),
        },
        {
            "id": "data_health",
            "title": "数据健康",
            "status": data_health["status"],
            "last_run_at": data_health["last_run_at"],
            "metrics": {
                "data_scope": _metric_value(freshness.get("data_scope")),
                "coverage_date": _metric_value(freshness.get("coverage_date") or freshness.get("latest_local_daily_date")),
                "latest_date_max": _metric_value(freshness.get("latest_date_max")),
                "fresh_count": _metric_value(freshness.get("fresh_count"), 0),
                "stale_count": _metric_value(freshness.get("stale_count"), 0),
                "missing_count": _metric_value(freshness.get("missing_count"), 0),
            },
            "symbol_lists": {
                "updated": freshness.get("updated_symbols") or freshness.get("latest_updated_symbols") or [],
                "stale": freshness.get("stale_symbols") or [],
                "missing": freshness.get("missing_symbols") or [],
                "scope_preview": freshness.get("symbols_preview") or [],
            },
            "scope_summary": _scope_display_fields({**health_raw, **freshness}),
            "notes": state_consistency.get("warnings") or [],
            "result_path": _first_path(data_health.get("output_paths")),
            "suggested_next_action": data_health.get("suggested_next_action"),
        },
        {
            "id": "intraday_coverage",
            "title": "分钟线覆盖",
            "status": "missing" if not intraday else ("warning" if float(intraday.get("coverage_rate") or 0) < 0.5 else "passed"),
            "last_run_at": data_health["last_run_at"],
            "metrics": {
                "coverage_rate": _metric_value(intraday.get("coverage_rate"), 0),
                "covered_count": _metric_value(intraday.get("covered_count"), 0),
                "missing_count": _metric_value(intraday.get("missing_count"), 0),
            },
            "result_path": "results/data_health/intraday_coverage_summary_latest.json",
            "suggested_next_action": intraday.get("suggested_next_action") or "缺少分钟线时不能验证盘中触发。",
        },
        {
            "id": "provider_status",
            "title": "Provider 状态",
            "status": provider_stats.get("status") or ("missing" if not provider_stats else "warning"),
            "last_run_at": data_health["last_run_at"],
            "metrics": {
                "provider_count": _metric_value(provider_stats.get("provider_count"), 0),
                "failed_count": _metric_value(provider_stats.get("failed_count"), 0),
            },
            "result_path": "results/data_health/provider_failure_stats_latest.json",
            "suggested_next_action": provider_stats.get("suggested_next_action") or "离线模式下 network_disabled 是 skipped，不是真实 provider 故障。",
        },
        {
            "id": "market_intelligence",
            "title": "市场观察",
            "status": summaries["market-intel"]["status"],
            "last_run_at": summaries["market-intel"]["last_run_at"],
            "metrics": {
                "market_regime": market_raw.get("market_intelligence_summary", {}).get("market_regime", "unknown"),
                "market_temperature": market_raw.get("market_intelligence_summary", {}).get("market_temperature", 0),
                "hot_sector_count": market_raw.get("market_intelligence_summary", {}).get("hot_sector_count", 0),
                "candidate_count": market_raw.get("market_intelligence_summary", {}).get("candidate_count", 0),
                "board_watch_count": market_raw.get("market_intelligence_summary", {}).get("board_watch_count", 0),
                "data_scope": market_raw.get("data_scope") or market_raw.get("scope") or "unknown",
                "hot_sectors": ", ".join([str(x) for x in (market_raw.get("market_intelligence_summary", {}).get("hot_sectors") or [])[:3]]) or "暂无",
                "top_candidates": ", ".join([str((x or {}).get("symbol") or "") for x in (market_raw.get("market_intelligence_summary", {}).get("top_candidates") or [])[:3]]) or "暂无",
            },
            "result_path": _first_path(summaries["market-intel"].get("output_paths")),
            "report_path": market_raw.get("report_path") or "reports/market_intelligence_report_latest.md",
            "scope_summary": _scope_display_fields(market_raw),
            "suggested_next_action": summaries["market-intel"].get("suggested_next_action"),
            "command": _spec_command("market-intel", overview_target),
        },
        {
            "id": "strategy_validation",
            "title": "策略验证",
            "status": summaries["strategy-validate"]["status"],
            "last_run_at": summaries["strategy-validate"]["last_run_at"],
            "metrics": {
                "lookahead_status": summaries["strategy-validate"].get("raw", {}).get("lookahead_status", "missing"),
                "execution_assumption_status": summaries["strategy-validate"].get("raw", {}).get("execution_assumption_status", "missing"),
            },
            "result_path": _first_path(summaries["strategy-validate"].get("output_paths")),
            "suggested_next_action": summaries["strategy-validate"].get("suggested_next_action"),
        },
        {
            "id": "backtest_credibility",
            "title": "回测可信度",
            "status": _worst_status(
                [
                    summaries["consistency-check"]["status"],
                    summaries["stats-validate"]["status"],
                    summaries["param-sensitivity"]["status"],
                ]
            ),
            "last_run_at": max(
                [
                    summaries["consistency-check"]["last_run_at"],
                    summaries["stats-validate"]["last_run_at"],
                    summaries["param-sensitivity"]["last_run_at"],
                ]
            ),
            "metrics": {
                "consistency": summaries["consistency-check"]["status"],
                "stats_validate": summaries["stats-validate"]["status"],
                "param_sensitivity": summaries["param-sensitivity"]["status"],
                "robustness_score": summaries["param-sensitivity"].get("raw", {}).get("robustness_score"),
            },
            "result_path": _first_path(summaries["param-sensitivity"].get("output_paths")),
            "suggested_next_action": summaries["param-sensitivity"].get("suggested_next_action"),
        },
        {
            "id": "learning_review",
            "title": "学习复盘",
            "status": _worst_status(
                [
                    summaries["observation-report"]["status"],
                    summaries["agent-report-audit"]["status"],
                    summaries["schema-check"]["status"],
                ]
            ),
            "last_run_at": max(
                [
                    summaries["observation-report"]["last_run_at"],
                    summaries["agent-report-audit"]["last_run_at"],
                    summaries["schema-check"]["last_run_at"],
                ]
            ),
            "metrics": {
                "observation_report": summaries["observation-report"]["status"],
                "agent_audit": summaries["agent-report-audit"]["status"],
                "schema_check": summaries["schema-check"]["status"],
                "confirmed_secret_leak_count": summaries["schema-check"].get("raw", {}).get("confirmed_secret_leak_count", 0),
                "secret_like_reference_count": summaries["schema-check"].get("raw", {}).get("secret_like_reference_count", 0),
            },
            "result_path": _first_path(summaries["observation-report"].get("output_paths")),
            "suggested_next_action": summaries["observation-report"].get("suggested_next_action"),
        },
    ]
    workflow = [
        ("self-check", "自检", "python scripts/self_check.py"),
        ("daily", "每日流程", _spec_command("daily", overview_target)),
        ("market-intel", "市场观察", _spec_command("market-intel", overview_target)),
        ("data-update", "更新 / 检查数据", _spec_command("data-update", overview_target)),
        ("data-health", "数据健康检查", _spec_command("data-health", overview_target)),
        ("daily-retain", "每日数据留存", _command_with_target("python scripts/qd.py daily-retain --symbols 600744,002115 --target-date YYYY-MM-DD --network false", overview_target)),
        ("strategy-validate", "策略验证", _spec_command("strategy-validate", overview_target)),
        ("consistency-check", "一致性检查", _spec_command("consistency-check", overview_target)),
        ("feature-audit", "特征 / 统计 / 参数审计", _spec_command("feature-audit", overview_target)),
        ("observation-report", "生成学习复盘报告", _spec_command("observation-report", overview_target)),
        ("archive-results", "归档结果", _command_with_target("python scripts/qd.py archive-results --target-date YYYY-MM-DD", overview_target)),
    ]
    command_rows = _command_lookup(root)
    workflow_reasons = {
        "self-check": "检查本地环境、版本、clean zip 规则和关键模块是否可用。",
        "data-update": "日线过期时用于离线检查本地状态；真正补数请使用 --enable-network。",
        "data-health": "刷新本地日线新鲜度、分钟线覆盖和 provider 状态。",
        "market-intel": "用当前分析池生成市场观察、热点板块和纸面观察候选。",
        "daily": "数据状态刷新后重新生成每日流程和报告。",
    }
    steps: list[dict[str, Any]] = []
    for key, title, command in workflow:
        summary = summaries.get(key)
        command_row = command_rows.get(key) or {}
        status = summary["status"] if summary else _status(command_row.get("last_status"), exists=bool(command_row))
        rendered_command = _command_with_target(command, overview_target)
        suggested = (summary or {}).get("suggested_next_action") or command_row.get("suggested_next_action") or rendered_command
        suggested = _command_with_target(str(suggested), overview_target)
        if status in {"missing", "unknown"} or "qd.py help" in str(suggested):
            suggested = rendered_command
        steps.append(
            {
                "id": key,
                "title": title,
                "status": status,
                "last_run_at": (summary or {}).get("last_run_at") or command_row.get("last_run_at") or "",
                "result_file": _first_path((summary or {}).get("output_paths") or command_row.get("output_paths") or []),
                "suggested_next_action": suggested,
                "command": rendered_command,
                "reason": workflow_reasons.get(key, "生成或刷新对应本地产物，便于后续查看。"),
            }
        )
    next_actions = build_workbench_next_actions(project_root=root, limit=3)
    overall = _worst_status([card["status"] for card in status_cards])
    warnings = []
    if not (root / "results").exists():
        warnings.append("尚未生成本地 results；请从首页今日流程开始。")
    if data_health["status"] == "missing":
        warnings.append("尚未运行 data-health。")
    if state_consistency.get("warnings"):
        warnings.append("状态文件口径不一致：" + "；".join(state_consistency.get("warnings") or []))
    return build_base_result(
        status="warning" if overall in {"warning", "failed", "missing", "partial"} else "passed",
        ok=True,
        warnings=warnings,
        suggested_next_action=(next_actions.get("actions") or [{}])[0].get("command", "python scripts/qd.py status"),
        data_update_summary={
            "status": data_update["status"],
            "data_scope": _metric_value(update_raw.get("data_scope")),
            "target_complete_trade_date": _metric_value(update_raw.get("target_complete_trade_date")),
            "effective_target_date": _metric_value(update_raw.get("effective_target_date") or update_raw.get("target_complete_trade_date")),
            "coverage_date": _metric_value(update_raw.get("coverage_date") or update_raw.get("latest_local_daily_date")),
            "latest_date_min": _metric_value(update_raw.get("latest_date_min")),
            "latest_date_max": _metric_value(update_raw.get("latest_date_max") or update_raw.get("latest_local_daily_max_date")),
            "latest_local_daily_date": _metric_value(update_raw.get("latest_local_daily_date")),
            "trading_day_lag": _metric_value(update_raw.get("trading_day_lag"), 0),
            "fresh_count": _metric_value(update_raw.get("fresh_count"), 0),
            "stale_count": _metric_value(update_raw.get("stale_count"), 0),
            "missing_count": _metric_value(update_raw.get("missing_count"), 0),
        },
        data_health_summary={
            "status": data_health["status"],
            "data_scope": _metric_value(freshness.get("data_scope")),
            "coverage_date": _metric_value(freshness.get("coverage_date") or freshness.get("latest_local_daily_date")),
            "latest_date_max": _metric_value(freshness.get("latest_date_max")),
            "stale_count": _metric_value(freshness.get("stale_count"), 0),
            "missing_count": _metric_value(freshness.get("missing_count"), 0),
            "data_grade_counts": grade.get("grade_counts", {}),
        },
        state_consistency=state_consistency,
        validation_summary=summaries["strategy-validate"],
        consistency_summary=summaries["consistency-check"],
        feature_quality_summary=summaries["feature-audit"],
        stats_validation_summary=summaries["stats-validate"],
        parameter_sensitivity_summary=summaries["param-sensitivity"],
        observation_summary=summaries["observation-report"],
        status_summary=summaries.get("status", {}),
        daily_summary=summaries.get("daily", {}),
        market_intelligence_summary=summaries.get("market-intel", {}),
        command_center_summary=summaries["command-center"],
        status_cards=status_cards,
        workflow_steps=steps,
        next_actions=next_actions.get("actions", []),
        network=False,
        llm_enabled=False,
        disclaimer=DISCLAIMER,
    )


def build_workbench_command_center(
    *,
    project_root: str | Path | None = None,
    recent: int = 20,
) -> dict[str, Any]:
    payload = _load_json("results/command_center_status_latest.json", project_root)
    target_date = _target_from_payload(payload)
    raw_commands = payload.get("commands") if isinstance(payload.get("commands"), list) else []
    commands = [
        _sanitize_command_fields(row, _target_from_payload(row, fallback=target_date))
        for row in raw_commands
        if isinstance(row, dict)
    ]
    timeline_path = _project_path("results/operation_log/operation_timeline.jsonl", project_root)
    timeline: list[dict[str, Any]] = []
    if timeline_path.exists():
        for line in timeline_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-recent:]:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                timeline.append(
                    {
                        "command": item.get("command", ""),
                        "status": item.get("status", ""),
                        "finished_at": item.get("finished_at") or item.get("started_at") or "",
                        "warnings": item.get("warnings") or [],
                        "failed_reason": item.get("failed_reason") or "",
                        "output_paths": item.get("output_paths") or [],
                    }
                )
    groups = {
        "基础": ["status", "fix-plan", "daily", "market-intel", "self-check", "providers", "data-update", "dev-preflight", "data-health", "daily-retain"],
        "验证": ["strategy-validate", "consistency-check", "feature-audit", "stats-validate", "param-sensitivity"],
        "报告": ["observation-report", "agent-report-audit", "schema-check"],
        "维护": ["manifest-check", "archive-results", "command-center", "llm-smoke", "network-smoke"],
    }
    command_map = {str(row.get("command")): row for row in commands if isinstance(row, dict)}
    grouped = {
        name: [command_map.get(command, {"command": command, "last_status": "missing", "output_paths": []}) for command in names]
        for name, names in groups.items()
    }
    status = payload.get("status") if payload else "missing"
    warnings = _warnings(payload) if payload else ["尚未记录操作历史。运行任意 qd.py 命令后会自动生成。"]
    return build_base_result(
        status=status,
        ok=True,
        warnings=warnings,
        suggested_next_action=_text_with_target_tokens(str(payload.get("suggested_next_action") or "python scripts/qd.py command-center"), target_date),
        commands=commands,
        grouped_commands=grouped,
        timeline=timeline,
        recent_operation_count=payload.get("recent_operation_count", len(timeline)) if payload else len(timeline),
        network=False,
        llm_enabled=False,
        disclaimer=DISCLAIMER,
    )


def _artifact_item(
    *,
    module: str,
    description: str,
    path: str,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    target = _project_path(path, project_root)
    exists = target.exists() and target.is_file()
    suffix = target.suffix.lower()
    preview = ""
    preview_type = "none"
    preview_summary: dict[str, Any] = {}
    preview_summary_text = ""
    preview_line_count = 0
    preview_truncated = False
    if exists and suffix in {".json", ".md", ".jsonl"}:
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            limit = 30 if suffix in {".json", ".jsonl"} else 80
            preview = "\n".join(lines[:limit])
            preview_type = "json" if suffix in {".json", ".jsonl"} else "markdown"
            preview_line_count = len(lines)
            preview_truncated = len(lines) > limit
            if preview_type == "json":
                try:
                    data = json.loads("\n".join(lines))
                except Exception:
                    data = {}
                if isinstance(data, dict):
                    remediation_actions = data.get("remediation_actions")
                    if not isinstance(remediation_actions, list):
                        plan = data.get("remediation_plan") if isinstance(data.get("remediation_plan"), dict) else {}
                        remediation_actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
                    preview_summary = {
                        "schema_version": data.get("schema_version") or data.get("api_schema_version") or "",
                        "app_version": data.get("app_version") or "",
                        "status": data.get("status") or data.get("workflow_status") or "",
                        "generated_at": data.get("generated_at") or data.get("created_at") or "",
                        "target_date": data.get("target_date") or data.get("target_complete_trade_date") or "",
                        "warning_count": data.get("warning_count") if data.get("warning_count") is not None else len(data.get("warnings") or []),
                        "failed_count": data.get("failed_count") if data.get("failed_count") is not None else len(data.get("errors") or []),
                        "remediation_action_count": len(remediation_actions),
                    }
            else:
                preview_summary_text = "\n".join([line for line in lines if line.strip()][:12])
        except Exception:
            preview = ""
            preview_type = "unavailable"
    remediation_map = {
        "daily": "python scripts/qd.py daily --network false --with-guardrails true",
        "market-intel": "python scripts/qd.py market-intel --target-date YYYY-MM-DD --price-limit 20 --network false",
        "mainline-radar": "python scripts/qd.py mainline-radar --network false",
        "status": "python scripts/qd.py status",
        "fix-plan": "python scripts/qd.py fix-plan",
        "command-center": "python scripts/qd.py command-center --summary true",
        "data-update": "python scripts/qd.py data-update --target-date YYYY-MM-DD --network false --scope current_analysis_pool",
        "data-health": "python scripts/qd.py data-health --target-date YYYY-MM-DD --network false --scope current_analysis_pool",
        "data-readiness": "python scripts/qd.py data-readiness --summary true",
        "data-gates": "python scripts/qd.py data-gates --summary true",
        "tushare-queue-status": "python scripts/qd.py tushare-queue-status --summary true",
        "tushare-minute-status": "python scripts/qd.py tushare-minute-status --summary true",
        "tushare-minute-coverage": "python scripts/qd.py tushare-minute-coverage --summary true",
        "tushare-retry-plan": "python scripts/qd.py tushare-retry-plan --summary true",
        "audit-extended-import": "python scripts/qd.py audit-extended-import --summary true --max-files-per-rule 0",
        "verify-extended-import": "python scripts/qd.py verify-extended-import --summary true",
        "daily-retain": "python scripts/qd.py daily-retain --network false",
        "strategy-validate": "python scripts/qd.py strategy-validate --target-date YYYY-MM-DD --network false",
        "consistency-check": "python scripts/qd.py consistency-check --target-date YYYY-MM-DD --network false",
        "feature-audit": "python scripts/qd.py feature-audit --target-date YYYY-MM-DD --network false",
        "stats-validate": "python scripts/qd.py stats-validate --target-date YYYY-MM-DD --network false",
        "param-sensitivity": "python scripts/qd.py param-sensitivity --target-date YYYY-MM-DD --network false --max-combinations 50 --seed 42",
        "observation-report": "python scripts/qd.py observation-report --target-date YYYY-MM-DD --network false --llm false --audit true",
        "schema-check": "python scripts/qd.py schema-check --results-dir results --network false",
        "archive-results": "python scripts/qd.py archive-results --target-date YYYY-MM-DD --dry-run true",
    }
    artifact_target = _target_from_payload(preview_summary, fallback=_target_from_path(path))
    remediation_command = _command_with_target(remediation_map.get(module, "python scripts/qd.py fix-plan"), artifact_target)
    remediation_network_required = "--enable-network" in remediation_command or "--network true" in remediation_command
    return {
        "module": module,
        "description": description,
        "path": _rel(target, project_root),
        "exists": exists,
        "updated_at": _mtime_text(target) if exists else "",
        "size_bytes": _file_size(target) if exists else 0,
        "status": "passed" if exists else "missing",
        "artifact_state": "已生成" if exists else "未生成",
        "remediation_command": remediation_command,
        "remediation_network_required": remediation_network_required,
        "remediation_title": "修复后重新生成" if exists else "生成该产物",
        "preview_type": preview_type,
        "preview": preview,
        "preview_summary": preview_summary,
        "preview_summary_text": preview_summary_text,
        "preview_line_count": preview_line_count,
        "preview_truncated": preview_truncated,
    }


def _latest_daily_retention_paths(project_root: str | Path | None = None) -> tuple[str, str]:
    root = _root(project_root)
    retention_root = root / "results" / "daily_retention"
    if not retention_root.exists():
        target = _current_complete_trade_date()
        return (
            f"results/daily_retention/{target}/daily_retention_manifest.json" if target else "results/daily_retention/daily_retention_manifest.json",
            f"results/daily_retention/{target}/daily_retention_summary.md" if target else "results/daily_retention/daily_retention_summary.md",
        )
    manifests = sorted(retention_root.glob("*/daily_retention_manifest.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    if not manifests:
        target = _current_complete_trade_date()
        return (
            f"results/daily_retention/{target}/daily_retention_manifest.json" if target else "results/daily_retention/daily_retention_manifest.json",
            f"results/daily_retention/{target}/daily_retention_summary.md" if target else "results/daily_retention/daily_retention_summary.md",
        )
    manifest = manifests[0]
    return (_rel(manifest, project_root), _rel(manifest.with_name("daily_retention_summary.md"), project_root))


def build_workbench_artifacts(*, project_root: str | Path | None = None) -> dict[str, Any]:
    manifest_path, daily_summary_path = _latest_daily_retention_paths(project_root)
    archive_target = _current_complete_trade_date()
    archive_manifest_path = (
        f"results/archive/{archive_target}/archive_manifest.json"
        if archive_target
        else "results/archive/archive_manifest.json"
    )
    groups: dict[str, list[dict[str, Any]]] = {
        "每日流程": [
            _artifact_item(module="daily", description="每日流程 Markdown 报告", path="reports/daily_quant_report_latest.md", project_root=project_root),
            _artifact_item(module="daily", description="每日流程 latest JSON", path="results/daily_workflow_latest.json", project_root=project_root),
            _artifact_item(module="status", description="QuantDesk 状态 JSON", path="results/status_latest.json", project_root=project_root),
            _artifact_item(module="status", description="QuantDesk 状态 Markdown 报告", path="reports/status_report_latest.md", project_root=project_root),
            _artifact_item(module="fix-plan", description="QuantDesk 修复计划 JSON", path="results/fix_plan_latest.json", project_root=project_root),
            _artifact_item(module="command-center", description="命令中心状态 JSON", path="results/command_center_status_latest.json", project_root=project_root),
        ],
        "市场观察": [
            _artifact_item(module="market-intel", description="市场观察总报告 Markdown", path="reports/market_intelligence_report_latest.md", project_root=project_root),
            _artifact_item(module="market-intel", description="市场观察总结果 JSON", path="results/market_intelligence/market_intelligence_latest.json", project_root=project_root),
            _artifact_item(module="market-intel", description="市场环境观察 JSON", path="results/market_intelligence/market_regime_latest.json", project_root=project_root),
            _artifact_item(module="market-intel", description="板块题材观察 JSON", path="results/market_intelligence/sector_theme_latest.json", project_root=project_root),
            _artifact_item(module="market-intel", description="消息面观察 JSON", path="results/market_intelligence/market_news_latest.json", project_root=project_root),
            _artifact_item(module="market-intel", description="低价候选观察 JSON", path="results/market_intelligence/sector_low_price_candidates_latest.json", project_root=project_root),
            _artifact_item(module="market-intel", description="情景观察曲线 JSON", path="results/market_intelligence/scenario_curves_latest.json", project_root=project_root),
        ],
        "数据更新": [
            _artifact_item(module="data-update", description="数据更新预检 JSON", path="results/data_update/data_update_latest.json", project_root=project_root),
            _artifact_item(module="data-update", description="数据更新 Markdown 报告", path="reports/data_update_report_latest.md", project_root=project_root),
        ],
        "数据健康": [
            _artifact_item(module="data-readiness", description="Extended Data Readiness JSON", path="results/data_health/extended_data_readiness_latest.json", project_root=project_root),
            _artifact_item(module="data-readiness", description="Extended Data Readiness Markdown", path="reports/extended_data_readiness_latest.md", project_root=project_root),
            _artifact_item(module="data-gates", description="Extended Data Gates JSON", path="results/data_health/extended_data_gates_latest.json", project_root=project_root),
            _artifact_item(module="data-gates", description="Extended Data Gates Markdown", path="reports/extended_data_gates_latest.md", project_root=project_root),
            _artifact_item(module="tushare-queue-status", description="Tushare Queue Status JSON", path="results/data_health/tushare_queue_status_latest.json", project_root=project_root),
            _artifact_item(module="tushare-queue-status", description="Tushare Queue Status Markdown", path="reports/tushare_queue_status_latest.md", project_root=project_root),
            _artifact_item(module="tushare-minute-status", description="Tushare Minute Status JSON", path="results/data_health/tushare_minute_status_latest.json", project_root=project_root),
            _artifact_item(module="tushare-minute-status", description="Tushare Minute Status Markdown", path="reports/tushare_minute_status_latest.md", project_root=project_root),
            _artifact_item(module="tushare-minute-coverage", description="Tushare Minute Coverage JSON", path="results/data_health/tushare_minute_coverage_latest.json", project_root=project_root),
            _artifact_item(module="tushare-minute-coverage", description="Tushare Minute Coverage Markdown", path="reports/tushare_minute_coverage_latest.md", project_root=project_root),
            _artifact_item(module="tushare-retry-plan", description="Tushare Retry Plan JSON", path="results/data_health/tushare_retry_plan_latest.json", project_root=project_root),
            _artifact_item(module="tushare-retry-plan", description="Tushare Retry Plan Markdown", path="reports/tushare_retry_plan_latest.md", project_root=project_root),
            _artifact_item(module="audit-extended-import", description="Extended Staged Import Audit JSON", path="results/data_health/extended_staged_import_audit_latest.json", project_root=project_root),
            _artifact_item(module="audit-extended-import", description="Extended Staged Import Audit Markdown", path="reports/extended_staged_import_audit_latest.md", project_root=project_root),
            _artifact_item(module="verify-extended-import", description="Extended Import Verification JSON", path="results/data_health/extended_import_verification_latest.json", project_root=project_root),
            _artifact_item(module="verify-extended-import", description="Extended Import Verification Markdown", path="reports/extended_import_verification_latest.md", project_root=project_root),
            _artifact_item(module="data-health", description="数据健康仪表盘 JSON", path="results/data_health/data_health_dashboard_latest.json", project_root=project_root),
            _artifact_item(module="data-health", description="数据健康 Markdown 报告", path="reports/data_health_dashboard_report_latest.md", project_root=project_root),
            _artifact_item(module="data-health", description="日线新鲜度摘要", path="results/data_health/data_freshness_summary_latest.json", project_root=project_root),
            _artifact_item(module="data-health", description="分钟线覆盖率摘要", path="results/data_health/intraday_coverage_summary_latest.json", project_root=project_root),
        ],
        "每日留存": [
            _artifact_item(module="daily-retain", description="每日留存 manifest", path=manifest_path, project_root=project_root),
            _artifact_item(module="daily-retain", description="每日留存摘要", path=daily_summary_path, project_root=project_root),
        ],
        "策略验证": [
            _artifact_item(module="strategy-validate", description="策略验证 latest JSON", path="results/strategy_validation_latest.json", project_root=project_root),
            _artifact_item(module="strategy-validate", description="策略验证 Markdown 报告", path="reports/strategy_validation_report_latest.md", project_root=project_root),
        ],
        "回测可信度": [
            _artifact_item(module="consistency-check", description="纸面组合与回测一致性 JSON", path="results/paper_vs_backtest_consistency_latest.json", project_root=project_root),
            _artifact_item(module="consistency-check", description="纸面组合与回测一致性报告", path="reports/paper_vs_backtest_consistency_report_latest.md", project_root=project_root),
        ],
        "特征/统计/参数审计": [
            _artifact_item(module="feature-audit", description="特征质量审计 JSON", path="results/feature_quality_audit_latest.json", project_root=project_root),
            _artifact_item(module="stats-validate", description="回测统计验证 JSON", path="results/backtest_statistical_validation_latest.json", project_root=project_root),
            _artifact_item(module="param-sensitivity", description="参数敏感性审计 JSON", path="results/parameter_sensitivity_latest.json", project_root=project_root),
        ],
        "学习复盘": [
            _artifact_item(module="observation-report", description="策略观察上下文 JSON", path="results/strategy_observation_context_latest.json", project_root=project_root),
            _artifact_item(module="observation-report", description="学习复盘报告 Markdown", path="results/strategy_observation_report_latest.md", project_root=project_root),
            _artifact_item(module="agent-audit", description="学习复盘审计 JSON", path="results/strategy_observation_report_audit_latest.json", project_root=project_root),
        ],
        "系统治理": [
            _artifact_item(module="schema-check", description="结果 schema 检查 JSON", path="results/schema_validation_latest.json", project_root=project_root),
            _artifact_item(module="command-center", description="命令中心状态 JSON", path="results/command_center_status_latest.json", project_root=project_root),
            _artifact_item(module="operation-log", description="操作时间线 JSONL", path="results/operation_log/operation_timeline.jsonl", project_root=project_root),
        ],
        "归档结果": [
            _artifact_item(module="archive-results", description="最近归档 manifest", path=archive_manifest_path, project_root=project_root),
        ],
    }
    flat = [item for items in groups.values() for item in items]
    missing = [item for item in flat if not item["exists"]]
    return build_base_result(
        status="warning" if missing else "passed",
        ok=True,
        warnings=["部分结果文件尚未生成，产物中心会显示为 missing。"] if missing else [],
        suggested_next_action="从首页今日流程运行对应 qd.py 命令生成结果。",
        groups=groups,
        artifact_count=len(flat),
        existing_count=len(flat) - len(missing),
        missing_count=len(missing),
        network=False,
        llm_enabled=False,
        disclaimer=DISCLAIMER,
    )
