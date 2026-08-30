"""Strategy correctness validation suite.

The suite combines lookahead diagnostics, execution assumption diagnostics,
local trading-calendar metadata, and existing local QuantDesk outputs.  It is
not a strategy optimizer and does not fetch market data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.backtest import default_backtest_assumptions
from quantdesk.execution_assumption_guard import (
    summarize_execution_assumption_issues,
    validate_execution_assumptions,
)
from quantdesk.shared import normalize_symbol, now_text, read_json, truthy, write_json
from quantdesk.strategy_lookahead_guard import (
    build_strategy_timeline_context,
    summarize_lookahead_issues,
    validate_strategy_no_lookahead,
)
from quantdesk.trading_calendar import calendar_info
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
STRATEGY_VALIDATION_LATEST_JSON = RESULTS_DIR / "strategy_validation_latest.json"
STRATEGY_VALIDATION_REPORT_LATEST_MD = REPORTS_DIR / "strategy_validation_report_latest.md"
DISCLAIMER = "仅用于本地学习研究和纸面模拟诊断，不构成投资建议。"


def _load(input_dir: Path, filename: str) -> dict[str, Any]:
    payload = read_json(input_dir / filename, {})
    return payload if isinstance(payload, dict) else {}


def collect_validation_inputs(
    *,
    symbol: str = "",
    target_date: str = "",
    input_dir: str | Path = RESULTS_DIR,
) -> dict[str, Any]:
    base = Path(input_dir)
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    observation = _load(base, "strategy_observation_context_latest.json")
    conditional_execution = _load(base, "conditional_execution_latest.json")
    self_validation_execution = _load(base, "conditional_execution_self_validation.json")
    daily_workflow = _load(base, "daily_workflow_latest.json")
    auto_strategy = _load(base, "auto_strategy_latest.json")
    provider_smoke = _load(base, "provider_network_smoke_latest.json")
    intraday_gap = _load(base, "intraday_gap_status_latest.json")
    paper_portfolio = _load(base, "auto_paper_portfolio_latest.json")

    execution_payload = conditional_execution or self_validation_execution
    target = target_date or str(observation.get("date") or daily_workflow.get("target_date") or now_text()[:10])
    normalized_symbol = normalize_symbol(symbol or observation.get("symbol") or "")
    return {
        "symbol": normalized_symbol,
        "target_date": target,
        "input_dir": str(base),
        "observation_context": observation,
        "strategy_payload": auto_strategy,
        "daily_workflow": daily_workflow,
        "provider_payload": provider_smoke,
        "intraday_status": intraday_gap or observation.get("intraday_status", {}),
        "execution_payload": execution_payload,
        "paper_portfolio": paper_portfolio,
        "loaded_files": {
            "strategy_observation_context": bool(observation),
            "conditional_execution": bool(conditional_execution),
            "conditional_execution_self_validation": bool(self_validation_execution),
            "daily_workflow": bool(daily_workflow),
            "auto_strategy": bool(auto_strategy),
            "provider_network_smoke": bool(provider_smoke),
            "intraday_gap_status": bool(intraday_gap),
            "auto_paper_portfolio": bool(paper_portfolio),
        },
    }


def _validation_warnings(inputs: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    loaded = inputs.get("loaded_files") if isinstance(inputs.get("loaded_files"), dict) else {}
    if not loaded.get("conditional_execution") and not loaded.get("conditional_execution_self_validation"):
        warnings.append("缺少本地条件执行结果，无法完整验证成交假设。")
    if not loaded.get("strategy_observation_context"):
        warnings.append("缺少 strategy_observation_context，仅能做有限验证。")
    if not loaded.get("intraday_gap_status"):
        warnings.append("缺少 intraday_gap_status，盘中触发验证能力未知。")
    return warnings


def run_strategy_validation_suite(
    *,
    symbol: str = "",
    target_date: str = "",
    input_dir: str | Path = RESULTS_DIR,
    output_dir: str | Path = RESULTS_DIR,
    report_dir: str | Path = REPORTS_DIR,
    network: bool = False,
    strict: bool = True,
    write: bool = True,
    output_format: str = "both",
) -> dict[str, Any]:
    inputs = collect_validation_inputs(symbol=symbol, target_date=target_date, input_dir=input_dir)
    target = str(inputs.get("target_date") or target_date or now_text()[:10])
    timeline_context = build_strategy_timeline_context(
        target_date=target,
        inputs=inputs.get("daily_workflow"),
        features=inputs.get("strategy_payload"),
        signals=inputs.get("daily_workflow"),
        executions=inputs.get("execution_payload"),
        provider_payload=inputs.get("provider_payload"),
        intraday_status=inputs.get("intraday_status"),
        report_payload=inputs.get("observation_context"),
    )
    lookahead = validate_strategy_no_lookahead(timeline_context, strict=strict)
    execution = validate_execution_assumptions(
        inputs.get("execution_payload"),
        assumptions=default_backtest_assumptions(),
        strict=strict,
    )
    cal_info = calendar_info(target_date=target)
    warnings = _validation_warnings(inputs)
    if cal_info.get("coverage_warning"):
        warnings.append(str(cal_info.get("coverage_warning")))
    if truthy(network):
        warnings.append("network=true 已收到，但 v3.2.3 strategy validation 不主动联网抓取行情。")

    total_issues = int(lookahead.get("issue_count") or 0) + int(execution.get("issue_count") or 0)
    total_warnings = len(lookahead.get("warnings") or []) + len(execution.get("warnings") or []) + len(warnings)
    status = "passed"
    if lookahead.get("status") == "failed" or execution.get("status") == "failed":
        status = "failed"
    elif total_issues or total_warnings:
        status = "warning"

    result = {
        "ok": status != "failed",
        "status": status,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "strategy_validation",
        "symbol": inputs.get("symbol"),
        "target_date": target,
        "network": bool(network),
        "strict": bool(strict),
        "calendar_info": cal_info,
        "calendar_quality": cal_info.get("calendar_quality"),
        "lookahead": lookahead,
        "execution_assumptions": execution,
        "issue_count": total_issues,
        "warning_count": total_warnings,
        "warnings": warnings,
        "input_files": inputs.get("loaded_files") or {},
        "summary": {
            "lookahead_status": lookahead.get("status"),
            "execution_assumption_status": execution.get("status"),
            "calendar_quality": cal_info.get("calendar_quality"),
            "issue_count": total_issues,
            "warning_count": total_warnings,
            "lookahead_score": lookahead.get("lookahead_score"),
            "execution_assumption_score": execution.get("execution_assumption_score"),
        },
        "checked_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_validation_report(result, output_dir=output_dir, report_dir=report_dir, output_format=output_format)
    return result


def build_validation_report(result: dict[str, Any]) -> str:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    lookahead = result.get("lookahead") if isinstance(result.get("lookahead"), dict) else {}
    execution = result.get("execution_assumptions") if isinstance(result.get("execution_assumptions"), dict) else {}
    warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
    lookahead_issues = lookahead.get("issues") if isinstance(lookahead.get("issues"), list) else []
    execution_issues = execution.get("issues") if isinstance(execution.get("issues"), list) else []
    execution_warnings = execution.get("warnings") if isinstance(execution.get("warnings"), list) else []
    lookahead_warnings = lookahead.get("warnings") if isinstance(lookahead.get("warnings"), list) else []

    def bullet(items: list[Any]) -> str:
        if not items:
            return "- 暂无。"
        lines = []
        for item in items[:30]:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('type')}` {item.get('message')} ({item.get('path') or '-'})")
            else:
                lines.append(f"- {item}")
        return "\n".join(lines)

    return f"""# QuantDesk 策略正确性验证报告

本报告只检查未来函数风险、盘中触发验证风险和纸面成交假设风险；不是收益优化器，不自动寻找高收益策略，不构成投资建议。

## 摘要

- symbol: `{result.get('symbol') or '-'}`
- target_date: `{result.get('target_date') or '-'}`
- status: `{result.get('status')}`
- lookahead_status: `{summary.get('lookahead_status')}`
- execution_assumption_status: `{summary.get('execution_assumption_status')}`
- calendar_quality: `{summary.get('calendar_quality')}`
- issue_count: `{result.get('issue_count')}`
- warning_count: `{result.get('warning_count')}`

## 未来函数检查

{summarize_lookahead_issues(lookahead)}

{bullet(lookahead_issues)}

## 未来函数检查警告

{bullet(lookahead_warnings)}

## 执行假设检查

{summarize_execution_assumption_issues(execution)}

{bullet(execution_issues)}

## 执行假设警告

{bullet(execution_warnings)}

## 数据与日历提示

{bullet(warnings)}

## 边界说明

QuantDesk 不接券商、不真实下单、不保存真实账户信息。缺少分钟线时无法验证盘中触发；打板候选纸面成交不能代表真实市场可成交。
"""


def write_validation_report(
    result: dict[str, Any],
    *,
    output_dir: str | Path = RESULTS_DIR,
    report_dir: str | Path = REPORTS_DIR,
    output_format: str = "both",
) -> dict[str, str]:
    out_dir = Path(output_dir)
    rep_dir = Path(report_dir)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir
    if not rep_dir.is_absolute():
        rep_dir = PROJECT_ROOT / rep_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "strategy_validation_latest.json"
    md_path = rep_dir / "strategy_validation_report_latest.md"
    fmt = str(output_format or "both").lower()
    paths: dict[str, str] = {}
    if fmt in {"json", "both"}:
        write_json(json_path, result)
        paths["json"] = str(json_path)
    if fmt in {"md", "both"}:
        md_path.write_text(build_validation_report(result), encoding="utf-8")
        paths["md"] = str(md_path)
    return paths


def summarize_validation_result(result: dict[str, Any]) -> str:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    return (
        f"lookahead_status={summary.get('lookahead_status')}; "
        f"execution_assumption_status={summary.get('execution_assumption_status')}; "
        f"calendar_quality={summary.get('calendar_quality')}; "
        f"issue_count={result.get('issue_count')}; warning_count={result.get('warning_count')}"
    )
