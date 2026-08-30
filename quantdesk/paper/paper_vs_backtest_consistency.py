"""Paper simulation versus backtest consistency diagnostics.

This module is read-only.  It compares existing local outputs and flags
contradictions between backtest assumptions, paper execution, strategy
validation, and daily-retention manifests.  It does not optimize strategies or
change any portfolio state.
"""

from __future__ import annotations

# Compatibility markers: run_paper_vs_backtest_consistency_check paper_vs_backtest_consistency_latest.json detect_reliable_event_without_intraday_evidence.
from pathlib import Path
from typing import Any

from quantdesk.backtest import default_backtest_assumptions
from quantdesk.execution_assumption_guard import (
    detect_board_candidate_fill_risk,
    detect_no_fill_state_change,
)
from quantdesk.shared import normalize_symbol, now_text, read_json, short_error_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
CONSISTENCY_LATEST_JSON = RESULTS_DIR / "paper_vs_backtest_consistency_latest.json"
CONSISTENCY_REPORT_LATEST_MD = REPORTS_DIR / "paper_vs_backtest_consistency_report_latest.md"
DISCLAIMER = "仅用于本地学习研究和纸面模拟诊断，不构成投资建议。"


def _load(path: str | Path) -> dict[str, Any]:
    payload = read_json(path, {})
    return payload if isinstance(payload, dict) else {}


def _records(value: Any, *, path: str = "input") -> list[tuple[str, dict[str, Any]]]:
    if value is None:
        return []
    if hasattr(value, "to_dict") and not isinstance(value, dict):
        try:
            return [(f"{path}[{i}]", row) for i, row in enumerate(value.to_dict(orient="records")) if isinstance(row, dict)]
        except Exception:
            return []
    if isinstance(value, dict):
        if any(key in value for key in ("action", "actual_action", "signal", "paper_action", "backtest_signal", "fill_model", "actual_fill_model")):
            return [(path, value)]
        rows: list[tuple[str, dict[str, Any]]] = []
        for key, item in value.items():
            rows.extend(_records(item, path=f"{path}.{key}"))
        return rows
    if isinstance(value, (list, tuple)):
        rows: list[tuple[str, dict[str, Any]]] = []
        for index, item in enumerate(value):
            rows.extend(_records(item, path=f"{path}[{index}]"))
        return rows
    return []


def _issue(issue_type: str, message: str, *, severity: str = "error", path: str = "", value: Any = None) -> dict[str, Any]:
    out = {"type": issue_type, "severity": severity, "message": message, "path": path}
    if value is not None:
        out["value"] = short_error_text(value)
    return out


def _action(row: dict[str, Any]) -> str:
    return str(row.get("paper_action") or row.get("actual_action") or row.get("action") or row.get("event") or "").strip().lower()


def _signal(row: dict[str, Any]) -> str:
    return str(row.get("backtest_signal") or row.get("signal") or row.get("strategy_signal") or row.get("expected_action") or "").strip().lower()


def _fill_model(row: dict[str, Any]) -> str:
    return str(row.get("fill_model") or row.get("actual_fill_model") or row.get("execution_model") or "").strip().lower()


def detect_signal_action_mismatch(backtest_payload: Any, paper_payload: Any) -> list[dict[str, Any]]:
    """Flag obvious signal/action contradictions when both sides are available."""

    issues: list[dict[str, Any]] = []
    backtest_rows = _records(backtest_payload, path="backtest")
    paper_rows = _records(paper_payload, path="paper")
    expected_by_symbol: dict[str, set[str]] = {}
    for _path, row in backtest_rows:
        symbol = normalize_symbol(row.get("symbol"))
        signal = _signal(row)
        if symbol and signal:
            expected_by_symbol.setdefault(symbol, set()).add(signal)
    for path, row in paper_rows:
        symbol = normalize_symbol(row.get("symbol"))
        action = _action(row)
        if not symbol or not action or action in {"no_fill", "missing_intraday", "hold"}:
            continue
        expected = expected_by_symbol.get(symbol)
        if expected and action not in expected and not (action in {"buy", "open", "entry"} and expected & {"buy", "open", "entry"}):
            issues.append(
                _issue(
                    "backtest_signal_paper_action_mismatch",
                    "paper action does not match available backtest signal for the same symbol",
                    path=path,
                    value={"symbol": symbol, "paper_action": action, "backtest_signals": sorted(expected)},
                )
            )
    return issues


def detect_reliable_event_without_intraday_evidence(execution_payload: Any, intraday_payload: Any = None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    available_symbols: set[str] = set()
    if isinstance(intraday_payload, dict):
        for key in ("available_symbols", "symbols_with_intraday"):
            values = intraday_payload.get(key)
            if isinstance(values, list):
                available_symbols.update(normalize_symbol(item) for item in values if normalize_symbol(item))
        for item in intraday_payload.get("items", []) if isinstance(intraday_payload.get("items"), list) else []:
            if isinstance(item, dict) and str(item.get("status") or "").lower() in {"available", "ok"}:
                symbol = normalize_symbol(item.get("symbol"))
                if symbol:
                    available_symbols.add(symbol)
    for path, row in _records(execution_payload, path="execution"):
        action = _action(row)
        fill_model = _fill_model(row)
        reliable = bool(row.get("is_reliable") or row.get("reliable") or row.get("reliable_simulated_event"))
        if action in {"", "no_fill"}:
            continue
        if reliable or fill_model in {"intraday_1m_close", "intraday_1m", "minute_1m"}:
            symbol = normalize_symbol(row.get("symbol"))
            evidence = row.get("intraday_evidence") or row.get("evidence_path") or row.get("minute_bar") or row.get("trigger_time")
            if not evidence and symbol not in available_symbols:
                issues.append(
                    _issue(
                        "reliable_event_missing_intraday_evidence",
                        "reliable simulated event should have minute-level evidence or an available intraday status",
                        path=path,
                        value=row,
                    )
                )
    return issues


def detect_missing_intraday_state_change(execution_payload: Any) -> list[dict[str, Any]]:
    return [
        {**issue, "type": "missing_intraday_state_change"}
        for issue in detect_no_fill_state_change(execution_payload)
        if "missing_intraday" in str(issue.get("value") or "").lower() or "missing_intraday" in str(issue.get("path") or "").lower()
    ]


def detect_missing_board_candidate_assumption(assumptions: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = assumptions if isinstance(assumptions, dict) else default_backtest_assumptions()
    if payload.get("board_candidate_execution"):
        return []
    return [
        _issue(
            "missing_board_candidate_execution_assumption",
            "backtest assumptions must include board_candidate_execution transparency text",
            severity="warning",
            path="assumptions.board_candidate_execution",
        )
    ]


def detect_daily_retention_manifest_mismatch(
    manifest_payload: dict[str, Any],
    *,
    strategy_validation_payload: dict[str, Any],
    provider_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not manifest_payload:
        return [
            _issue(
                "daily_retention_manifest_missing",
                "daily retention manifest is missing; consistency with retained run cannot be verified",
                severity="warning",
                path="results/daily_retention",
            )
        ]
    if manifest_payload.get("app_version") and manifest_payload.get("app_version") != APP_VERSION:
        issues.append(_issue("manifest_app_version_mismatch", "daily retention manifest app_version differs from current version", severity="warning", path="manifest.app_version"))
    if manifest_payload.get("package_version") and manifest_payload.get("package_version") != PACKAGE_VERSION:
        issues.append(_issue("manifest_package_version_mismatch", "daily retention manifest package_version differs from current version", severity="warning", path="manifest.package_version"))
    manifest_validation = manifest_payload.get("strategy_validation") if isinstance(manifest_payload.get("strategy_validation"), dict) else {}
    current_summary = strategy_validation_payload.get("summary") if isinstance(strategy_validation_payload.get("summary"), dict) else {}
    for key in ("lookahead_status", "execution_assumption_status", "calendar_quality"):
        if manifest_validation.get(key) and current_summary.get(key) and manifest_validation.get(key) != current_summary.get(key):
            issues.append(
                _issue(
                    "manifest_strategy_validation_mismatch",
                    "daily retention manifest strategy_validation differs from current validation summary",
                    severity="warning",
                    path=f"manifest.strategy_validation.{key}",
                    value={"manifest": manifest_validation.get(key), "current": current_summary.get(key)},
                )
            )
    manifest_chain = manifest_payload.get("provider_chain") if isinstance(manifest_payload.get("provider_chain"), list) else []
    current_chain = provider_payload.get("steps") if isinstance(provider_payload.get("steps"), list) else []
    if manifest_chain and current_chain and len(manifest_chain) != len(current_chain):
        issues.append(
            _issue(
                "manifest_provider_chain_mismatch",
                "daily retention manifest provider_chain length differs from current provider smoke steps",
                severity="warning",
                path="manifest.provider_chain",
                value={"manifest": len(manifest_chain), "current": len(current_chain)},
            )
        )
    return issues


def _default_manifest_path(target_date: str) -> Path:
    return RESULTS_DIR / "daily_retention" / str(target_date or now_text()[:10]) / "daily_retention_manifest.json"


def run_paper_vs_backtest_consistency_check(
    *,
    symbol: str = "",
    target_date: str = "",
    input_dir: str | Path = RESULTS_DIR,
    output_dir: str | Path = RESULTS_DIR,
    report_dir: str | Path = REPORTS_DIR,
    network: bool = False,
    strict: bool = True,
    write: bool = True,
    backtest_payload: dict[str, Any] | None = None,
    paper_payload: dict[str, Any] | None = None,
    execution_payload: dict[str, Any] | None = None,
    intraday_payload: dict[str, Any] | None = None,
    strategy_validation_payload: dict[str, Any] | None = None,
    manifest_payload: dict[str, Any] | None = None,
    provider_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = Path(input_dir)
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    target = str(target_date or now_text()[:10])
    backtest = backtest_payload if isinstance(backtest_payload, dict) else _load(base / "backtest_latest.json")
    paper = paper_payload if isinstance(paper_payload, dict) else _load(base / "auto_paper_portfolio_latest.json")
    execution = execution_payload if isinstance(execution_payload, dict) else _load(base / "conditional_execution_latest.json") or _load(base / "conditional_execution_self_validation.json")
    intraday = intraday_payload if isinstance(intraday_payload, dict) else _load(base / "intraday_gap_status_latest.json")
    validation = strategy_validation_payload if isinstance(strategy_validation_payload, dict) else _load(base / "strategy_validation_latest.json")
    provider = provider_payload if isinstance(provider_payload, dict) else _load(base / "provider_network_smoke_latest.json")
    manifest = manifest_payload if isinstance(manifest_payload, dict) else _load(_default_manifest_path(target))

    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    issues.extend(detect_signal_action_mismatch(backtest, paper or execution))
    issues.extend(detect_no_fill_state_change(execution))
    issues.extend(detect_missing_intraday_state_change(execution))
    issues.extend(detect_reliable_event_without_intraday_evidence(execution, intraday))
    warnings.extend(detect_board_candidate_fill_risk(execution))
    warnings.extend(detect_missing_board_candidate_assumption(default_backtest_assumptions()))
    for item in detect_daily_retention_manifest_mismatch(manifest, strategy_validation_payload=validation, provider_payload=provider):
        if item.get("severity") == "warning":
            warnings.append(item)
        else:
            issues.append(item)
    if network:
        warnings.append(_issue("network_ignored", "network=true was provided, but consistency-check is local-only in v3.2.5", severity="warning"))

    status = "passed"
    if issues and strict:
        status = "failed"
    elif issues or warnings:
        status = "warning"
    result = {
        "ok": status != "failed",
        "status": status,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "paper_vs_backtest_consistency",
        "symbol": normalize_symbol(symbol),
        "target_date": target,
        "network": bool(network),
        "strict": bool(strict),
        "issue_count": len(issues),
        "warning_count": len(warnings),
        "issues": issues,
        "warnings": warnings,
        "summary": {
            "consistency_status": status,
            "issue_count": len(issues),
            "warning_count": len(warnings),
            "manifest_available": bool(manifest),
            "strategy_validation_status": validation.get("status"),
        },
        "checked_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_consistency_outputs(result, output_dir=output_dir, report_dir=report_dir)
    return result


def build_consistency_report(result: dict[str, Any]) -> str:
    def bullet(items: list[dict[str, Any]]) -> str:
        if not items:
            return "- 暂无。"
        return "\n".join(f"- `{item.get('type')}` {item.get('message')} ({item.get('path') or '-'})" for item in items[:40])

    return f"""# QuantDesk Paper Vs Backtest Consistency Report

本报告只检查本地纸面模拟、回测、分钟线执行、策略验证和每日留存 manifest 之间是否存在明显矛盾；不优化收益、不修改策略、不接券商、不真实下单、不构成投资建议。

## Summary

- symbol: `{result.get('symbol') or '-'}`
- target_date: `{result.get('target_date') or '-'}`
- status: `{result.get('status')}`
- issue_count: `{result.get('issue_count')}`
- warning_count: `{result.get('warning_count')}`

## Issues

{bullet(result.get('issues') if isinstance(result.get('issues'), list) else [])}

## Warnings

{bullet(result.get('warnings') if isinstance(result.get('warnings'), list) else [])}
"""


def write_consistency_outputs(
    result: dict[str, Any],
    *,
    output_dir: str | Path = RESULTS_DIR,
    report_dir: str | Path = REPORTS_DIR,
) -> dict[str, str]:
    out = Path(output_dir)
    rep = Path(report_dir)
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    if not rep.is_absolute():
        rep = PROJECT_ROOT / rep
    json_path = out / "paper_vs_backtest_consistency_latest.json"
    report_path = rep / "paper_vs_backtest_consistency_report_latest.md"
    write_json(json_path, result)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_consistency_report(result), encoding="utf-8")
    result["json_path"] = str(json_path)
    result["report_path"] = str(report_path)
    return {"json_path": str(json_path), "report_path": str(report_path)}


def summarize_consistency_result(result: dict[str, Any]) -> str:
    return (
        f"consistency_status={result.get('status')}; "
        f"issue_count={result.get('issue_count')}; "
        f"warning_count={result.get('warning_count')}; "
        f"manifest_available={(result.get('summary') or {}).get('manifest_available')}"
    )
