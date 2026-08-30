"""Daily data retention and reproducibility manifest workflow.

This module keeps a local, auditable record of what QuantDesk knew on a target
date.  It is diagnostic only: defaults are offline, it never scans full A by
default, and it does not change strategy logic, scores, or paper portfolio
execution rules.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack
from quantdesk.agent.agent_report_audit import build_report_audit_record, write_report_audit
from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report
from quantdesk.data_update_orchestrator import build_stale_data_warning
from quantdesk.llm_config import load_llm_config
from quantdesk.provider_network_smoke import run_provider_network_smoke
from quantdesk.reproducibility_manifest import (
    DISCLAIMER,
    build_reproducibility_manifest,
    compute_file_hash,
    validate_reproducibility_manifest,
)
from quantdesk.research.backtest_statistical_validation import summarize_statistical_validation
from quantdesk.research.feature_quality_audit import summarize_feature_quality
from quantdesk.research.parameter_sensitivity_audit import summarize_parameter_sensitivity
from quantdesk.shared import normalize_symbol, now_text, read_json, short_error_text, truthy, write_json
from quantdesk.strategy_observation_contract import build_strategy_observation_context
from quantdesk.strategy_validation_suite import run_strategy_validation_suite
from quantdesk.trading_calendar import calendar_info
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
LOW_PRICE_UNIVERSE_CSV = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"
DEFAULT_OUTPUT_DIR = RESULTS_DIR / "daily_retention"
FEATURE_QUALITY_LATEST_JSON = RESULTS_DIR / "feature_quality_audit_latest.json"
STATISTICAL_VALIDATION_LATEST_JSON = RESULTS_DIR / "backtest_statistical_validation_latest.json"
DATA_HEALTH_DASHBOARD_LATEST_JSON = RESULTS_DIR / "data_health" / "data_health_dashboard_latest.json"
PARAMETER_SENSITIVITY_LATEST_JSON = RESULTS_DIR / "parameter_sensitivity_latest.json"


def _rel(path: str | Path) -> str:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target)


def _split_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        symbol = normalize_symbol(item)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _load_low_price_symbols(max_symbols: int = 3, *, universe_path: str | Path = LOW_PRICE_UNIVERSE_CSV) -> list[str]:
    path = Path(universe_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        return []
    symbols: list[str] = []
    limit = max(0, int(max_symbols or 0)) or 3
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if "included" in row and not truthy(row.get("included")):
                    continue
                symbol = normalize_symbol(row.get("symbol") or row.get("code"))
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
                if len(symbols) >= limit:
                    break
    except Exception:
        return []
    return symbols


def build_retention_plan(
    *,
    symbols: Any = None,
    target_date: str = "",
    network: bool = False,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    include_provider_smoke: bool = True,
    include_strategy_validation: bool = True,
    include_observation_report: bool = True,
    llm: bool = False,
    max_symbols: int = 3,
    command_line_args: Any = None,
    low_price_universe_path: str | Path = LOW_PRICE_UNIVERSE_CSV,
) -> dict[str, Any]:
    explicit = _split_symbols(symbols)
    selected = explicit or _load_low_price_symbols(max_symbols=max_symbols, universe_path=low_price_universe_path)
    selected = selected[: max(0, int(max_symbols or 0)) or 3] if not explicit else selected
    target = str(target_date or now_text()[:10])
    base_output = Path(output_dir)
    if not base_output.is_absolute():
        base_output = PROJECT_ROOT / base_output
    day_dir = base_output / target
    warnings: list[str] = []
    if not explicit and not selected:
        warnings.append("未传入 symbols，且低价学习池没有可用 included=true 样本；不会默认扫描全 A。")
    return {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "target_date": target,
        "symbols": selected,
        "symbol_source": "explicit" if explicit else "low_price_included_sample",
        "max_symbols": int(max_symbols or 0) or 3,
        "full_scan": False,
        "network": bool(network),
        "llm_enabled": bool(llm),
        "include_provider_smoke": bool(include_provider_smoke),
        "include_strategy_validation": bool(include_strategy_validation),
        "include_observation_report": bool(include_observation_report),
        "output_dir": _rel(base_output),
        "day_dir": _rel(day_dir),
        "manifest_path": _rel(day_dir / "daily_retention_manifest.json"),
        "summary_path": _rel(day_dir / "daily_retention_summary.md"),
        "command_line_args": [str(item) for item in command_line_args] if isinstance(command_line_args, (list, tuple)) else ([str(command_line_args)] if command_line_args else []),
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }


def _data_file_status(symbols: list[str], *, kind: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        if kind == "daily":
            candidates = [PROJECT_ROOT / "data" / "daily" / f"{symbol}.csv", PROJECT_ROOT / "data" / "daily" / f"{symbol}_qfq.parquet"]
        else:
            candidates = [
                PROJECT_ROOT / "data" / "intraday" / "1m" / f"{symbol}.csv",
                PROJECT_ROOT / "data" / "intraday" / f"{symbol}.csv",
            ]
        existing = [path for path in candidates if path.exists()]
        rows.append(
            {
                "symbol": symbol,
                "status": "available" if existing else "missing",
                "paths": [_rel(path) for path in existing],
                "hashes": [compute_file_hash(path) for path in existing],
            }
        )
    return {
        "kind": kind,
        "available_count": sum(1 for row in rows if row["status"] == "available"),
        "missing_count": sum(1 for row in rows if row["status"] == "missing"),
        "items": rows,
    }


def _summarize_data_health(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        return {
            "status": "missing",
            "freshness_status": "missing",
            "stale_count": 0,
            "missing_daily_count": 0,
            "intraday_coverage_rate": None,
            "provider_failure_summary": {},
            "data_grade_counts": {},
            "warnings": ["missing"],
        }
    summary = payload.get("data_health_summary") if isinstance(payload.get("data_health_summary"), dict) else {}
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
    intraday = payload.get("intraday") if isinstance(payload.get("intraday"), dict) else {}
    provider = payload.get("provider_failure_stats") if isinstance(payload.get("provider_failure_stats"), dict) else {}
    grade = payload.get("data_grade_trend") if isinstance(payload.get("data_grade_trend"), dict) else {}
    return {
        "status": summary.get("status") or payload.get("status") or "unknown",
        "freshness_status": summary.get("freshness_status") or freshness.get("status") or "missing",
        "stale_count": int(summary.get("stale_count") if summary.get("stale_count") is not None else freshness.get("stale_count") or 0),
        "missing_daily_count": int(summary.get("missing_daily_count") if summary.get("missing_daily_count") is not None else freshness.get("missing_count") or 0),
        "intraday_coverage_rate": summary.get("intraday_coverage_rate") if summary.get("intraday_coverage_rate") is not None else intraday.get("coverage_rate"),
        "provider_failure_summary": summary.get("provider_failure_summary") if isinstance(summary.get("provider_failure_summary"), dict) else {
            "failed_count": int(provider.get("failed_count") or 0),
            "skipped_count": int(provider.get("skipped_count") or 0),
            "offline_skipped_count": int(provider.get("offline_skipped_count") or 0),
        },
        "data_grade_counts": summary.get("data_grade_counts") if isinstance(summary.get("data_grade_counts"), dict) else (grade.get("grade_counts") if isinstance(grade.get("grade_counts"), dict) else {}),
        "warnings": payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
    }


def collect_daily_retention_snapshot(plan: dict[str, Any]) -> dict[str, Any]:
    symbols = _split_symbols(plan.get("symbols") or [])
    target_date = str(plan.get("target_date") or now_text()[:10])
    network = bool(plan.get("network"))
    warnings = [short_error_text(item) for item in plan.get("warnings", []) if item]
    day_dir = PROJECT_ROOT / str(plan.get("day_dir") or f"results/daily_retention/{target_date}")
    day_dir.mkdir(parents=True, exist_ok=True)

    cal_info = calendar_info(target_date=target_date)
    provider_smoke: dict[str, Any] = {"status": "skipped", "summary": {}, "steps": []}
    if plan.get("include_provider_smoke"):
        provider_smoke = run_provider_network_smoke(
            symbols=symbols,
            target_date=target_date,
            network=network,
            limit=int(plan.get("max_symbols") or 3),
            write=True,
        )

    strategy_validation: dict[str, Any] = {"status": "skipped", "summary": {}}
    if plan.get("include_strategy_validation"):
        strategy_validation = run_strategy_validation_suite(
            symbol=symbols[0] if symbols else "",
            target_date=target_date,
            network=network,
            strict=True,
            write=True,
            output_format="both",
        )

    observation_context: dict[str, Any] = {}
    llm_result: dict[str, Any] = {}
    audit_record: dict[str, Any] = {}
    observation_paths: dict[str, str] = {}
    if plan.get("include_observation_report"):
        context_path = RESULTS_DIR / "strategy_observation_context_latest.json"
        report_path = RESULTS_DIR / "strategy_observation_report_latest.md"
        llm_result_path = RESULTS_DIR / "strategy_observation_llm_result_latest.json"
        audit_path = RESULTS_DIR / "strategy_observation_report_audit_latest.json"
        observation_context = build_strategy_observation_context(
            symbol=symbols[0] if symbols else "",
            target_date=target_date,
            network=False,
            write=True,
            output_path=context_path,
            validation_payload=strategy_validation if isinstance(strategy_validation, dict) else None,
            provider_payload=provider_smoke if isinstance(provider_smoke, dict) else None,
        )
        llm_config = load_llm_config(None)
        llm_config["llm_enabled"] = bool(plan.get("llm_enabled"))
        llm_result = generate_llm_observation_report(observation_context, llm_config, network=network and bool(plan.get("llm_enabled")))
        report_path.write_text(str(llm_result.get("report") or ""), encoding="utf-8")
        write_json(llm_result_path, {k: v for k, v in llm_result.items() if k != "raw_response"})
        evidence_pack = build_agent_evidence_pack(observation_context)
        audit_record = build_report_audit_record(observation_context, evidence_pack, llm_result)
        write_report_audit(audit_record, audit_path)
        observation_paths = {
            "observation_context_path": _rel(context_path),
            "observation_report_path": _rel(report_path),
            "llm_result_path": _rel(llm_result_path),
            "agent_audit_path": _rel(audit_path),
        }

    feature_quality_payload = read_json(FEATURE_QUALITY_LATEST_JSON, {})
    feature_quality_summary = (
        summarize_feature_quality(feature_quality_payload)
        if isinstance(feature_quality_payload, dict) and feature_quality_payload
        else {"status": "missing", "high_missing_features": [], "high_missing_feature_count": 0}
    )
    statistical_validation_payload = read_json(STATISTICAL_VALIDATION_LATEST_JSON, {})
    statistical_validation_summary = (
        summarize_statistical_validation(statistical_validation_payload)
        if isinstance(statistical_validation_payload, dict) and statistical_validation_payload
        else {"status": "missing", "p_value": None, "bootstrap_ci_summary": {}, "warnings": ["missing"]}
    )
    data_health_payload = read_json(DATA_HEALTH_DASHBOARD_LATEST_JSON, {})
    data_health_summary = _summarize_data_health(data_health_payload if isinstance(data_health_payload, dict) else {})
    parameter_sensitivity_payload = read_json(PARAMETER_SENSITIVITY_LATEST_JSON, {})
    parameter_sensitivity_summary = (
        summarize_parameter_sensitivity(parameter_sensitivity_payload)
        if isinstance(parameter_sensitivity_payload, dict) and parameter_sensitivity_payload
        else {"status": "missing", "robustness_score": None, "combination_count": 0, "warning_count": 0, "top_overfit_warnings": []}
    )
    stale_data_warning = build_stale_data_warning(
        target_date=target_date,
        symbols=symbols,
        max_symbols=int(plan.get("max_symbols") or len(symbols) or 80),
    )
    if stale_data_warning.get("stale"):
        warnings.append(str(stale_data_warning.get("message") or "本地日线数据未更新到目标完整交易日，学习复盘可信度下降。"))

    daily_status = _data_file_status(symbols, kind="daily")
    intraday_status = _data_file_status(symbols, kind="intraday")
    provider_chain = provider_smoke.get("steps") if isinstance(provider_smoke.get("steps"), list) else []
    data_source_trace = observation_context.get("data_source_trace") if isinstance(observation_context.get("data_source_trace"), dict) else []
    outputs = [
        RESULTS_DIR / "provider_network_smoke_latest.json",
        RESULTS_DIR / "strategy_validation_latest.json",
        PROJECT_ROOT / "reports" / "strategy_validation_report_latest.md",
        RESULTS_DIR / "strategy_observation_context_latest.json",
        RESULTS_DIR / "strategy_observation_report_latest.md",
        RESULTS_DIR / "strategy_observation_llm_result_latest.json",
        RESULTS_DIR / "strategy_observation_report_audit_latest.json",
        FEATURE_QUALITY_LATEST_JSON,
        PROJECT_ROOT / "reports" / "feature_quality_audit_report_latest.md",
        STATISTICAL_VALIDATION_LATEST_JSON,
        PROJECT_ROOT / "reports" / "backtest_statistical_validation_report_latest.md",
        DATA_HEALTH_DASHBOARD_LATEST_JSON,
        PROJECT_ROOT / "reports" / "data_health_dashboard_report_latest.md",
        PARAMETER_SENSITIVITY_LATEST_JSON,
        PROJECT_ROOT / "reports" / "parameter_sensitivity_report_latest.md",
    ]
    inputs = [
        PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv",
        LOW_PRICE_UNIVERSE_CSV,
        PROJECT_ROOT / "config" / "llm_config.json",
        PROJECT_ROOT / "config" / "provider_secrets.json",
    ]
    manifest = build_reproducibility_manifest(
        target_date=target_date,
        symbols=symbols,
        network=network,
        llm_enabled=bool(plan.get("llm_enabled")),
        calendar_quality=str(cal_info.get("calendar_quality") or ""),
        commands=plan.get("command_line_args") or [],
        inputs=inputs,
        outputs=[path for path in outputs if Path(path).exists()],
        provider_chain=provider_chain,
        data_source_trace=data_source_trace,
        strategy_validation=strategy_validation.get("summary") if isinstance(strategy_validation.get("summary"), dict) else strategy_validation,
        feature_quality_summary=feature_quality_summary,
        statistical_validation_summary=statistical_validation_summary,
        data_health_summary=data_health_summary,
        parameter_sensitivity_summary=parameter_sensitivity_summary,
        warnings=warnings,
    )
    manifest["stale_data_warning"] = stale_data_warning
    return {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "daily_data_retention",
        "target_date": target_date,
        "symbols": symbols,
        "network": network,
        "llm_enabled": bool(plan.get("llm_enabled")),
        "full_scan": False,
        "calendar_info": cal_info,
        "calendar_quality": cal_info.get("calendar_quality"),
        "provider_smoke_summary": provider_smoke.get("summary") if isinstance(provider_smoke, dict) else {},
        "provider_chain": provider_chain,
        "data_source_trace": data_source_trace,
        "daily_data_status": daily_status,
        "intraday_data_status": intraday_status,
        "strategy_validation_summary": strategy_validation.get("summary") if isinstance(strategy_validation.get("summary"), dict) else {},
        "feature_quality_summary": feature_quality_summary,
        "statistical_validation_summary": statistical_validation_summary,
        "data_health_summary": data_health_summary,
        "parameter_sensitivity_summary": parameter_sensitivity_summary,
        "stale_data_warning": stale_data_warning,
        "observation_context_path": observation_paths.get("observation_context_path", ""),
        "observation_report_path": observation_paths.get("observation_report_path", ""),
        "agent_audit_path": observation_paths.get("agent_audit_path", ""),
        "command_line_args": plan.get("command_line_args") or [],
        "generated_at": now_text(),
        "warnings": warnings,
        "manifest": manifest,
        "disclaimer": DISCLAIMER,
    }


def write_retention_manifest(snapshot: dict[str, Any], output_path: str | Path | None = None) -> Path:
    target = Path(output_path) if output_path else PROJECT_ROOT / "results" / "daily_retention" / str(snapshot.get("target_date") or now_text()[:10]) / "daily_retention_manifest.json"
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    manifest = snapshot.get("manifest") if isinstance(snapshot.get("manifest"), dict) else {}
    write_json(target, manifest)
    snapshot["manifest_path"] = _rel(target)
    snapshot["manifest_validation"] = validate_reproducibility_manifest(manifest)
    return target


def build_retention_summary(snapshot: dict[str, Any]) -> str:
    provider_summary = snapshot.get("provider_smoke_summary") if isinstance(snapshot.get("provider_smoke_summary"), dict) else {}
    validation_summary = snapshot.get("strategy_validation_summary") if isinstance(snapshot.get("strategy_validation_summary"), dict) else {}
    feature_summary = snapshot.get("feature_quality_summary") if isinstance(snapshot.get("feature_quality_summary"), dict) else {}
    stats_summary = snapshot.get("statistical_validation_summary") if isinstance(snapshot.get("statistical_validation_summary"), dict) else {}
    data_health_summary = snapshot.get("data_health_summary") if isinstance(snapshot.get("data_health_summary"), dict) else {}
    parameter_summary = snapshot.get("parameter_sensitivity_summary") if isinstance(snapshot.get("parameter_sensitivity_summary"), dict) else {}
    return "\n".join(
        [
            "# QuantDesk Daily Data Retention Summary",
            "",
            "仅用于本地学习研究、数据留存和复现检查，不构成投资建议。",
            "",
            f"- target_date: `{snapshot.get('target_date') or '-'}`",
            f"- symbols: `{', '.join(snapshot.get('symbols') or []) or '-'}`",
            f"- network: `{snapshot.get('network')}`",
            f"- llm_enabled: `{snapshot.get('llm_enabled')}`",
            f"- calendar_quality: `{snapshot.get('calendar_quality') or '-'}`",
            f"- provider ok/skipped/failed: `{provider_summary.get('ok', 0)}/{provider_summary.get('skipped', 0)}/{provider_summary.get('failed', 0)}`",
            f"- strategy validation: `{validation_summary.get('lookahead_status') or '-'} / {validation_summary.get('execution_assumption_status') or '-'}`",
            f"- feature quality: `{feature_summary.get('status') or 'missing'}` high_missing=`{feature_summary.get('high_missing_feature_count', 0)}`",
            f"- statistical validation: `{stats_summary.get('status') or 'missing'}` p_value=`{stats_summary.get('p_value')}`",
            f"- data health: `{data_health_summary.get('status') or 'missing'}` freshness=`{data_health_summary.get('freshness_status') or 'missing'}` intraday_coverage=`{data_health_summary.get('intraday_coverage_rate')}`",
            f"- parameter sensitivity: `{parameter_summary.get('status') or 'missing'}` robustness_score=`{parameter_summary.get('robustness_score')}` combinations=`{parameter_summary.get('combination_count')}`",
            f"- daily missing: `{(snapshot.get('daily_data_status') or {}).get('missing_count')}`",
            f"- intraday missing: `{(snapshot.get('intraday_data_status') or {}).get('missing_count')}`",
            f"- manifest: `{snapshot.get('manifest_path') or '-'}`",
            "",
            "## Warnings",
            *(f"- {item}" for item in (snapshot.get("warnings") or [])),
        ]
    )


def write_retention_summary(snapshot: dict[str, Any], output_path: str | Path | None = None) -> Path:
    target = Path(output_path) if output_path else PROJECT_ROOT / "results" / "daily_retention" / str(snapshot.get("target_date") or now_text()[:10]) / "daily_retention_summary.md"
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_retention_summary(snapshot), encoding="utf-8")
    snapshot["summary_path"] = _rel(target)
    return target


def load_retention_manifest(path: str | Path) -> dict[str, Any]:
    payload = read_json(path, {})
    return payload if isinstance(payload, dict) else {}


def validate_retention_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return validate_reproducibility_manifest(manifest)


def summarize_retention_result(snapshot: dict[str, Any]) -> str:
    provider_summary = snapshot.get("provider_smoke_summary") if isinstance(snapshot.get("provider_smoke_summary"), dict) else {}
    validation_summary = snapshot.get("strategy_validation_summary") if isinstance(snapshot.get("strategy_validation_summary"), dict) else {}
    feature_summary = snapshot.get("feature_quality_summary") if isinstance(snapshot.get("feature_quality_summary"), dict) else {}
    stats_summary = snapshot.get("statistical_validation_summary") if isinstance(snapshot.get("statistical_validation_summary"), dict) else {}
    data_health_summary = snapshot.get("data_health_summary") if isinstance(snapshot.get("data_health_summary"), dict) else {}
    parameter_summary = snapshot.get("parameter_sensitivity_summary") if isinstance(snapshot.get("parameter_sensitivity_summary"), dict) else {}
    return (
        f"target_date={snapshot.get('target_date')}; symbols={','.join(snapshot.get('symbols') or []) or '-'}; "
        f"network={snapshot.get('network')}; calendar_quality={snapshot.get('calendar_quality')}; "
        f"provider_failed={provider_summary.get('failed', 0)}; "
        f"strategy_validation={validation_summary.get('lookahead_status') or '-'}/{validation_summary.get('execution_assumption_status') or '-'}; "
        f"feature_quality={feature_summary.get('status') or 'missing'}; "
        f"statistical_validation={stats_summary.get('status') or 'missing'}; "
        f"data_health={data_health_summary.get('status') or 'missing'}; "
        f"parameter_sensitivity={parameter_summary.get('status') or 'missing'}; "
        f"manifest={snapshot.get('manifest_path') or '-'}; summary={snapshot.get('summary_path') or '-'}"
    )
