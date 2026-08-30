"""Archive latest diagnostic outputs into a dated local folder."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from quantdesk.operation_log import sanitize_for_operation_log
from quantdesk.shared import now_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISCLAIMER = "仅用于本地结果归档和复盘定位，不构成投资建议。"


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target)


def build_archive_plan(
    *,
    target_date: str,
    results_dir: str | Path = "results",
    reports_dir: str | Path = "reports",
    archive_dir: str | Path = "results/archive",
    include_reports: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    date_text = str(target_date or "unknown-date")
    results = _project_path(results_dir)
    reports = _project_path(reports_dir)
    archive_root = _project_path(archive_dir) / date_text
    entries: list[dict[str, str]] = [
        {"source": _rel(results / "strategy_validation_latest.json"), "dest": "results/strategy_validation_latest.json"},
        {"source": _rel(results / "paper_vs_backtest_consistency_latest.json"), "dest": "results/paper_vs_backtest_consistency_latest.json"},
        {"source": _rel(results / "feature_quality_audit_latest.json"), "dest": "results/feature_quality_audit_latest.json"},
        {"source": _rel(results / "backtest_statistical_validation_latest.json"), "dest": "results/backtest_statistical_validation_latest.json"},
        {"source": _rel(results / "parameter_sensitivity_latest.json"), "dest": "results/parameter_sensitivity_latest.json"},
        {"source": _rel(results / "schema_validation_latest.json"), "dest": "results/schema_validation_latest.json"},
        {"source": _rel(results / "daily_workflow_latest.json"), "dest": "results/daily_workflow_latest.json"},
        {"source": _rel(results / "market_intelligence" / "market_intelligence_latest.json"), "dest": "results/market_intelligence/market_intelligence_latest.json"},
        {"source": _rel(results / "market_intelligence" / "market_regime_latest.json"), "dest": "results/market_intelligence/market_regime_latest.json"},
        {"source": _rel(results / "market_intelligence" / "sector_theme_latest.json"), "dest": "results/market_intelligence/sector_theme_latest.json"},
        {"source": _rel(results / "market_intelligence" / "market_news_latest.json"), "dest": "results/market_intelligence/market_news_latest.json"},
        {"source": _rel(results / "market_intelligence" / "sector_low_price_candidates_latest.json"), "dest": "results/market_intelligence/sector_low_price_candidates_latest.json"},
        {"source": _rel(results / "market_intelligence" / "scenario_curves_latest.json"), "dest": "results/market_intelligence/scenario_curves_latest.json"},
        {"source": _rel(results / "status_latest.json"), "dest": "results/status_latest.json"},
        {"source": _rel(results / "command_center_status_latest.json"), "dest": "results/command_center_status_latest.json"},
        {"source": _rel(results / "data_update" / "data_update_latest.json"), "dest": "results/data_update/data_update_latest.json"},
        {"source": _rel(results / "data_health" / "data_health_dashboard_latest.json"), "dest": "results/data_health/data_health_dashboard_latest.json"},
        {"source": _rel(results / "data_health" / "data_freshness_summary_latest.json"), "dest": "results/data_health/data_freshness_summary_latest.json"},
        {"source": _rel(results / "data_health" / "intraday_coverage_summary_latest.json"), "dest": "results/data_health/intraday_coverage_summary_latest.json"},
        {"source": _rel(results / "data_health" / "provider_failure_stats_latest.json"), "dest": "results/data_health/provider_failure_stats_latest.json"},
        {"source": _rel(results / "data_health" / "data_grade_trend_latest.json"), "dest": "results/data_health/data_grade_trend_latest.json"},
        {"source": _rel(results / "strategy_observation_context_latest.json"), "dest": "results/strategy_observation_context_latest.json"},
        {"source": _rel(results / "strategy_observation_report_latest.md"), "dest": "results/strategy_observation_report_latest.md"},
        {"source": _rel(results / "strategy_observation_report_audit_latest.json"), "dest": "results/strategy_observation_report_audit_latest.json"},
        {"source": _rel(results / "provider_network_smoke_latest.json"), "dest": "results/provider_network_smoke_latest.json"},
        {"source": _rel(results / "llm_observation_agent_smoke_latest.json"), "dest": "results/llm_observation_agent_smoke_latest.json"},
        {
            "source": _rel(results / "daily_retention" / date_text / "daily_retention_manifest.json"),
            "dest": "results/daily_retention_manifest.json",
        },
        {
            "source": _rel(results / "daily_retention" / date_text / "daily_retention_summary.md"),
            "dest": "results/daily_retention_summary.md",
        },
    ]
    if include_reports:
        entries.extend(
            [
                {"source": _rel(reports / "strategy_validation_report_latest.md"), "dest": "reports/strategy_validation_report_latest.md"},
                {
                    "source": _rel(reports / "paper_vs_backtest_consistency_report_latest.md"),
                    "dest": "reports/paper_vs_backtest_consistency_report_latest.md",
                },
                {"source": _rel(reports / "feature_quality_audit_report_latest.md"), "dest": "reports/feature_quality_audit_report_latest.md"},
                {
                    "source": _rel(reports / "backtest_statistical_validation_report_latest.md"),
                    "dest": "reports/backtest_statistical_validation_report_latest.md",
                },
                {"source": _rel(reports / "parameter_sensitivity_report_latest.md"), "dest": "reports/parameter_sensitivity_report_latest.md"},
                {"source": _rel(reports / "schema_validation_report_latest.md"), "dest": "reports/schema_validation_report_latest.md"},
                {"source": _rel(reports / "daily_quant_report_latest.md"), "dest": "reports/daily_quant_report_latest.md"},
                {"source": _rel(reports / "market_intelligence_report_latest.md"), "dest": "reports/market_intelligence_report_latest.md"},
                {"source": _rel(reports / "market_regime_report_latest.md"), "dest": "reports/market_regime_report_latest.md"},
                {"source": _rel(reports / "sector_theme_report_latest.md"), "dest": "reports/sector_theme_report_latest.md"},
                {"source": _rel(reports / "market_news_report_latest.md"), "dest": "reports/market_news_report_latest.md"},
                {"source": _rel(reports / "sector_low_price_candidates_report_latest.md"), "dest": "reports/sector_low_price_candidates_report_latest.md"},
                {"source": _rel(reports / "scenario_curves_report_latest.md"), "dest": "reports/scenario_curves_report_latest.md"},
                {"source": _rel(reports / "status_report_latest.md"), "dest": "reports/status_report_latest.md"},
                {"source": _rel(reports / "data_update_report_latest.md"), "dest": "reports/data_update_report_latest.md"},
                {"source": _rel(reports / "data_health_dashboard_report_latest.md"), "dest": "reports/data_health_dashboard_report_latest.md"},
            ]
        )
    return {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "target_date": date_text,
        "archive_path": _rel(archive_root),
        "manifest_path": _rel(archive_root / "archive_manifest.json"),
        "include_reports": bool(include_reports),
        "dry_run": bool(dry_run),
        "entries": entries,
        "disclaimer": DISCLAIMER,
    }


def copy_result_if_exists(source: str | Path, destination: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    src = _project_path(source)
    dst = _project_path(destination)
    if not src.exists() or not src.is_file():
        return {"source": _rel(src), "destination": _rel(dst), "status": "skipped", "reason": "missing"}
    if dry_run:
        return {"source": _rel(src), "destination": _rel(dst), "status": "planned", "size": src.stat().st_size}
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"source": _rel(src), "destination": _rel(dst), "status": "archived", "size": dst.stat().st_size}


def archive_latest_outputs(plan: dict[str, Any]) -> dict[str, Any]:
    archive_root = _project_path(plan.get("archive_path") or "results/archive/unknown-date")
    dry_run = bool(plan.get("dry_run"))
    rows: list[dict[str, Any]] = []
    for entry in plan.get("entries") or []:
        source = entry.get("source") or ""
        dest = archive_root / str(entry.get("dest") or Path(source).name)
        rows.append(copy_result_if_exists(source, dest, dry_run=dry_run))
    archived_count = sum(1 for row in rows if row.get("status") in {"archived", "planned"})
    skipped_count = sum(1 for row in rows if row.get("status") == "skipped")
    payload = {
        **plan,
        "ok": True,
        "status": "planned" if dry_run else "passed",
        "generated_at": now_text(),
        "archived_count": archived_count,
        "skipped_count": skipped_count,
        "items": rows,
        "warnings": ["部分 latest 文件不存在，已记录 skipped。"] if skipped_count else [],
    }
    return sanitize_for_operation_log(payload)


def write_archive_manifest(payload: dict[str, Any], output_path: str | Path | None = None) -> Path | None:
    if payload.get("dry_run"):
        return None
    target = _project_path(output_path or payload.get("manifest_path") or "results/archive/unknown-date/archive_manifest.json")
    return write_json(target, payload)


def summarize_archive_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_date": payload.get("target_date"),
        "dry_run": bool(payload.get("dry_run")),
        "archived_count": int(payload.get("archived_count") or 0),
        "skipped_count": int(payload.get("skipped_count") or 0),
        "archive_path": payload.get("archive_path"),
        "manifest_path": payload.get("manifest_path"),
        "status": payload.get("status"),
        "warnings": payload.get("warnings") or [],
    }
