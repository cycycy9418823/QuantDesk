"""Human-sized console summary for the QuantDesk daily workflow.

The summary is intentionally advisory and local-only. It does not alter the
daily workflow payload, strategy logic, scoring formulas, or backtest returns.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from quantdesk.remediation_advisor import build_remediation_plan

DISCLAIMER = "仅用于本地研究复盘和流程诊断，不构成投资建议。"


def safe_get_daily_value(payload: dict[str, Any] | None, *keys: str, default: Any = "暂无数据") -> Any:
    current: Any = payload if isinstance(payload, dict) else {}
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current.get(key)
        else:
            return default
    if current in (None, ""):
        return default
    return current


def _status(payload: dict[str, Any]) -> str:
    text = str(payload.get("status") or payload.get("workflow_status") or "").strip().lower()
    if text in {"completed", "passed", "ok", "success"}:
        return "passed"
    if text == "partial":
        return "warning"
    if text in {"warning", "failed", "missing", "skipped", "unavailable"}:
        return text
    return "warning" if payload else "missing"


def _status_label(status: str) -> str:
    return {
        "passed": "passed",
        "warning": "warning",
        "failed": "failed",
        "missing": "missing",
        "skipped": "skipped",
        "unavailable": "unavailable",
    }.get(status, status or "missing")


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


def _command_with_optional_target(command: str, target_date: str = "") -> str:
    text = " ".join(str(command or "").strip().split())
    if not text:
        return ""
    target = _iso_date_text(target_date)
    if target:
        return text.replace("YYYY-MM-DD", target)
    return (
        text.replace(" --target-date YYYY-MM-DD", "")
        .replace("--target-date YYYY-MM-DD ", "")
        .replace("--target-date YYYY-MM-DD", "")
        .strip()
    )


def _rel_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "暂无数据"
    try:
        path = Path(text)
        if path.is_absolute():
            root = Path(__file__).resolve().parents[1]
            return str(path.relative_to(root)).replace("\\", "/")
    except Exception:
        pass
    return text.replace("\\", "/")


def format_daily_status_line(payload: dict[str, Any]) -> str:
    return _status_label(_status(payload))


def format_daily_guardrail_summary(guardrails: dict[str, Any] | None) -> str:
    guardrails = guardrails if isinstance(guardrails, dict) else {}
    if guardrails.get("enabled") is False:
        return "已跳过"
    warning_count = _int(guardrails.get("warning_count"))
    failed_count = _int(guardrails.get("failed_count"))
    return f"{warning_count} warning / {failed_count} failed"


def format_daily_artifact_paths(payload: dict[str, Any]) -> dict[str, str]:
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    report_path = (
        artifacts.get("daily_report")
        or payload.get("report_path")
        or artifacts.get("daily_workflow_report")
        or "reports/daily_quant_report_latest.md"
    )
    result_path = artifacts.get("daily_result") or payload.get("latest_path") or "results/daily_workflow_latest.json"
    return {
        "report_path": _rel_path(report_path),
        "result_path": _rel_path(result_path),
    }


def build_daily_next_actions(payload: dict[str, Any]) -> list[dict[str, str]]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    daily_price = payload.get("daily_price_status") if isinstance(payload.get("daily_price_status"), dict) else {}
    artifacts = format_daily_artifact_paths(payload)
    target = (
        _iso_date_text(summary.get("target_complete_trade_date"))
        or _iso_date_text(daily_price.get("target_date"))
        or _iso_date_text(payload.get("target_date"))
    )
    actions = [
        {
            "category": "review",
            "reason": "先查看每日流程 Markdown 报告。",
            "command": str(artifacts["report_path"]),
        }
    ]
    stale_count = _int(summary.get("daily_price_stale_count") or daily_price.get("stale_count"))
    missing_count = _int(summary.get("daily_price_missing_count") or daily_price.get("missing_count"))
    no_universe = _int(daily_price.get("total_symbols") or daily_price.get("symbol_count")) == 0 or str(
        summary.get("data_freshness_status") or daily_price.get("data_freshness_status") or ""
    ) == "no_universe"
    if no_universe:
        actions.append(
            {
                "category": "data",
                "reason": "当前没有可分析股票，无法更新 current_analysis_pool；clean zip 首次运行请先初始化 demo 股票池。",
                "command": "python scripts/qd.py data-bootstrap --preset low_price_demo --network false",
            }
        )
    elif stale_count or missing_count or bool(daily_price.get("is_stale")):
        actions.append(
            {
                "category": "data",
                "reason": "日线存在 stale/missing；联网补齐日线需要显式授权。",
                "command": _command_with_optional_target(
                    "python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network --scope current_analysis_pool",
                    target,
                ),
            }
        )
    else:
        actions.append(
            {
                "category": "validation",
                "reason": "数据状态可读后，可刷新 data-health 或继续复盘。",
                "command": _command_with_optional_target(
                    "python scripts/qd.py data-health --target-date YYYY-MM-DD --network false --scope current_analysis_pool",
                    target,
                ),
            }
        )
    actions.append(
        {
            "category": "review",
            "reason": "需要学习复盘时运行离线 observation-report。",
            "command": _command_with_optional_target(
                "python scripts/qd.py observation-report --target-date YYYY-MM-DD --network false --llm false --audit true",
                target,
            ),
        }
    )
    return actions[:3]


def build_daily_console_summary(payload: dict[str, Any]) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    daily_price = payload.get("daily_price_status") if isinstance(payload.get("daily_price_status"), dict) else {}
    guardrails = payload.get("guardrail_summary") if isinstance(payload.get("guardrail_summary"), dict) else {}
    remediation_plan = payload.get("remediation_plan") if isinstance(payload.get("remediation_plan"), dict) else build_remediation_plan(daily_payload=payload)
    remediation_actions = remediation_plan.get("actions") if isinstance(remediation_plan.get("actions"), list) else []
    artifacts = format_daily_artifact_paths(payload)
    warning_count = _int(guardrails.get("warning_count"), len(payload.get("warnings") or []))
    failed_count = _int(guardrails.get("failed_count"), len(payload.get("errors") or []))
    candidate_count = _int(summary.get("long_candidate_count")) + _int(summary.get("board_candidate_count"))
    high_risk_count = _int(summary.get("high_risk_candidate_count"))
    status = _status(payload)
    next_actions = build_daily_next_actions(payload)
    return {
        "status": status,
        "headline": "QuantDesk 每日流程完成" if status != "failed" else "QuantDesk 每日流程失败",
        "target_date": summary.get("target_complete_trade_date") or daily_price.get("target_date") or "暂无数据",
        "data_scope": payload.get("scope") or daily_price.get("data_scope") or "low_price_included_only",
        "symbol_count": safe_get_daily_value(daily_price, "total_symbols", default=safe_get_daily_value(payload, "low_price_status", "included_count", default=0)),
        "coverage_date": daily_price.get("coverage_date") or daily_price.get("latest_date_min") or daily_price.get("latest_local_date") or "暂无数据",
        "latest_date_max": daily_price.get("latest_date_max") or daily_price.get("max_local_date") or summary.get("local_daily_latest_date") or "暂无数据",
        "candidate_count": candidate_count,
        "high_risk_candidate_count": high_risk_count,
        "warning_count": warning_count,
        "failed_count": failed_count,
        "guardrails_text": format_daily_guardrail_summary(guardrails),
        "report_path": artifacts["report_path"],
        "result_path": artifacts["result_path"],
        "next_actions": next_actions,
        "remediation_actions": remediation_actions[:3],
        "disclaimer": DISCLAIMER,
    }


def format_daily_console_summary(payload: dict[str, Any]) -> str:
    summary = payload.get("daily_console_summary") if isinstance(payload.get("daily_console_summary"), dict) else build_daily_console_summary(payload)
    actions = summary.get("remediation_actions") if isinstance(summary.get("remediation_actions"), list) else []
    if not actions:
        actions = summary.get("next_actions") if isinstance(summary.get("next_actions"), list) else []
    lines = [
        str(summary.get("headline") or "QuantDesk 每日流程完成"),
        "",
        f"日期：{summary.get('target_date') or '暂无数据'}",
        f"状态：{summary.get('status') or '暂无数据'}",
        f"数据范围：{summary.get('data_scope') or '暂无数据'} / {summary.get('symbol_count') or 0} 只",
        f"数据覆盖：全池覆盖到 {summary.get('coverage_date') or '暂无数据'}，最晚已有日线 {summary.get('latest_date_max') or '暂无数据'}",
        f"候选数量：{summary.get('candidate_count') or 0}",
        f"高风险候选：{summary.get('high_risk_candidate_count') or 0}",
        f"Guardrails：{summary.get('guardrails_text') or '暂无数据'}",
        f"报告：{summary.get('report_path') or '暂无数据'}",
        f"结果：{summary.get('result_path') or '暂无数据'}",
        "",
        "我现在该怎么修：",
    ]
    for index, action in enumerate(actions[:3], start=1):
        title = action.get("title") or action.get("reason") or "继续查看本地结果。"
        command = action.get("command") or ""
        follow_up = action.get("follow_up_command") or ""
        lines.append(f"{index}. {title}")
        if action.get("impact"):
            lines.append(f"   影响：{action.get('impact')}")
        lines.append(f"   {'需要显式联网授权' if action.get('network_required') else '离线可运行，不会联网'}")
        lines.append(f"   运行：{command}")
        if follow_up:
            lines.append("   后续命令：离线可运行，不会联网")
            lines.append(f"   然后：{follow_up}")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
