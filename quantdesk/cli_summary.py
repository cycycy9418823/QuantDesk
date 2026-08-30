from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quantdesk.shared import now_text, truthy
from quantdesk.version import APP_VERSION

DEFAULT_DISCLAIMER = (
    "Local QuantDesk operator summary only. It preserves warnings and does not "
    "connect to brokers, place real orders, or provide investment advice."
)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _clean_text(value: Any, fallback: str) -> str:
    text = "" if value is None else str(value)
    if "\ufffd" in text or "????" in text or "???" in text:
        return fallback
    return text


def _count(value: Any) -> int:
    if isinstance(value, Mapping):
        return len(value)
    if isinstance(value, (list, tuple, set)):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return truthy(value)


def _payload_ok(payload: Mapping[str, Any]) -> bool:
    if "ok" in payload:
        return _bool_value(payload.get("ok"), False)
    return str(payload.get("status") or "").lower() != "failed"


def _warnings(payload: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    for item in _as_list(payload.get("warnings")):
        if item:
            warnings.append(_clean_text(item, "warning text contained unreadable characters; inspect full JSON."))
    for key in ("warning", "failed_reason", "skipped_reason"):
        value = payload.get(key)
        if value:
            warnings.append(_clean_text(value, "warning text contained unreadable characters; inspect full JSON."))
    warning_kind = payload.get("warning_kind")
    if warning_kind and not warnings:
        warnings.append(_clean_text(warning_kind, "warning_kind_unreadable"))
    return list(dict.fromkeys(warnings))


def _output_paths(payload: Mapping[str, Any]) -> dict[str, Any]:
    paths = _as_dict(payload.get("output_paths"))
    if payload.get("output_path") and "json" not in paths:
        paths["json"] = payload.get("output_path")
    if payload.get("report_path") and "report" not in paths:
        paths["report"] = payload.get("report_path")
    return paths


def _target_date(payload: Mapping[str, Any]) -> str:
    nested_summary = _as_dict(payload.get("summary"))
    daily_console = _as_dict(payload.get("daily_console_summary"))
    daily_price = _as_dict(payload.get("daily_price_status"))
    freshness = _as_dict(payload.get("freshness") or payload.get("data_freshness_gate"))
    for value in (
        payload.get("target_date"),
        payload.get("target_complete_trade_date"),
        payload.get("trade_date"),
        payload.get("end_date"),
        daily_console.get("target_date"),
        nested_summary.get("target_complete_trade_date"),
        nested_summary.get("daily_price_target_date"),
        daily_price.get("target_date"),
        freshness.get("target_complete_trade_date"),
        freshness.get("target_date"),
    ):
        text = str(value or "").strip()
        if text and text not in {"-", "YYYY-MM-DD", "暂无数据"}:
            return text[:10]
    return ""


def _metrics_preview(metrics: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("total_return", "annual_return", "max_drawdown", "sharpe", "win_rate", "final_equity")
    return {key: metrics.get(key) for key in keys if key in metrics}


def _generic_key_counts(payload: Mapping[str, Any]) -> dict[str, Any]:
    key_counts: dict[str, Any] = {}
    for key in (
        "symbol_count",
        "selected_count",
        "analysis_pool_count",
        "total_scanned",
        "total_matched",
        "trade_count",
        "candidate_count",
        "board_watch_count",
        "hot_sector_count",
        "updated_daily_count",
    ):
        if key in payload:
            key_counts[key] = payload.get(key)
    symbols = payload.get("symbols")
    if "symbols_count" not in key_counts and isinstance(symbols, list):
        key_counts["symbols_count"] = len(symbols)
    return key_counts


def build_cli_summary(payload: Mapping[str, Any], *, task_type: str | None = None, max_warnings: int = 5) -> dict[str, Any]:
    """Build compact operator-readable JSON from a full QuantDesk payload.

    The summary is intentionally lossy for long arrays, but it keeps status,
    warnings, output paths, next action, and safety disclaimers. Full results
    remain in each command's stable latest JSON file.
    """

    task = task_type or str(payload.get("task_type") or "unknown")
    warnings = _warnings(payload)
    summary: dict[str, Any] = {
        "ok": _payload_ok(payload),
        "status": payload.get("status") or "unknown",
        "warning_kind": payload.get("warning_kind") or "",
        "task_type": task,
        "generated_at": payload.get("generated_at") or now_text(),
        "app_version": payload.get("app_version") or APP_VERSION,
        "target_date": _target_date(payload),
        "network": _bool_value(payload.get("network"), False),
        "warning_count": len(warnings),
        "warnings_top": warnings[: max(1, max_warnings)],
        "key_counts": _generic_key_counts(payload),
        "output_paths": _output_paths(payload),
        "suggested_next_action": _clean_text(
            payload.get("suggested_next_action") or "",
            "Inspect output_paths and rerun with full JSON for detailed next action.",
        ),
        "disclaimer": _clean_text(payload.get("disclaimer") or DEFAULT_DISCLAIMER, DEFAULT_DISCLAIMER),
    }

    if task in {"data_health_dashboard", "data-health"}:
        freshness = _as_dict(payload.get("freshness"))
        health = _as_dict(payload.get("data_health_summary"))
        stale = int(health.get("stale_count") or freshness.get("stale_count") or 0)
        missing = int(health.get("missing_daily_count") or freshness.get("missing_count") or 0)
        ready = int(freshness.get("fresh_count") or 0)
        current_gate = str(health.get("extended_data_current_gate") or "")
        gate_action = str(health.get("extended_data_gate_suggested_next_action") or "")
        queue_ok_count = int(health.get("extended_data_queue_ok_count") or 0)
        queue_fail_count = int(health.get("extended_data_queue_fail_count") or 0)
        queue_traceback_count = int(health.get("extended_data_queue_traceback_count") or 0)
        queue_stdout_age = health.get("extended_data_queue_stdout_age_seconds")
        queue_stderr_age = health.get("extended_data_queue_stderr_age_seconds")
        queue_liveness = str(health.get("extended_data_queue_liveness") or "")
        queue_progress_state = str(health.get("extended_data_queue_progress_state") or "")
        queue_stage_progress = str(health.get("extended_data_queue_stage_progress") or "")
        queue_stage_requested_count = int(health.get("extended_data_queue_stage_requested_count") or 0)
        queue_stage_completed_count = int(health.get("extended_data_queue_stage_completed_count") or 0)
        queue_stage_remaining_count = int(health.get("extended_data_queue_stage_remaining_count") or 0)
        queue_stage_remaining_keys = [
            str(item) for item in _as_list(health.get("extended_data_queue_stage_remaining_keys")) if str(item).strip()
        ]
        queue_stage_current_elapsed = health.get("extended_data_queue_stage_current_elapsed_seconds")
        queue_stage_last_event_age = health.get("extended_data_queue_stage_last_event_age_seconds")
        queue_stage_last_event_status = str(health.get("extended_data_queue_stage_last_event_status") or "")
        queue_ok_delta = health.get("extended_data_queue_ok_delta")
        queue_fail_delta = health.get("extended_data_queue_fail_delta")
        queue_traceback_delta = health.get("extended_data_queue_traceback_delta")
        minute_status = str(health.get("tushare_minute_status") or "")
        minute_done_count = int(health.get("tushare_minute_symbol_done_count") or 0)
        minute_symbol_count = int(health.get("tushare_minute_symbol_count") or 0)
        minute_last_symbol = str(health.get("tushare_minute_last_symbol") or "")
        minute_traceback_count = int(health.get("tushare_minute_traceback_count") or 0)
        minute_status_action = str(health.get("tushare_minute_suggested_next_action") or "")
        minute_coverage_status = str(health.get("tushare_minute_coverage_status") or "")
        minute_target_symbol_count = int(health.get("tushare_minute_target_symbol_count") or 0)
        minute_current_available_count = int(health.get("tushare_minute_current_available_count") or 0)
        minute_current_available_count_source = str(health.get("tushare_minute_current_available_count_source") or "")
        minute_canonical_current_available_count = int(
            health.get("tushare_minute_canonical_current_available_count") or 0
        )
        minute_staged_current_not_imported_count = int(
            health.get("tushare_minute_staged_current_not_imported_count") or 0
        )
        minute_canonical_current_available_ratio = health.get("tushare_minute_canonical_current_available_ratio")
        minute_staged_current_not_imported_ratio = health.get("tushare_minute_staged_current_not_imported_ratio")
        minute_missing_or_stale_count = int(health.get("tushare_minute_missing_or_stale_count") or 0)
        minute_current_available_ratio = health.get("tushare_minute_current_available_ratio")
        minute_coverage_action = str(health.get("tushare_minute_coverage_suggested_next_action") or "")
        minute_canonical_file_count = int(health.get("tushare_minute_canonical_file_count") or 0)
        minute_completed_job_path = str(health.get("tushare_minute_latest_completed_job_path") or "")
        minute_completed_job_symbol_count = int(health.get("tushare_minute_latest_completed_job_symbol_count") or 0)
        minute_completed_job_current_count = int(
            health.get("tushare_minute_latest_completed_job_canonical_current_count") or 0
        )
        minute_completed_job_missing_count = int(
            health.get("tushare_minute_latest_completed_job_missing_or_stale_count") or 0
        )
        minute_completed_quality_status = str(health.get("tushare_minute_latest_completed_quality_status") or "")
        minute_completed_quality_total = int(health.get("tushare_minute_latest_completed_quality_total_symbols") or 0)
        minute_completed_quality_usable = int(health.get("tushare_minute_latest_completed_quality_usable_count") or 0)
        minute_completed_quality_invalid = int(health.get("tushare_minute_latest_completed_quality_invalid_count") or 0)
        minute_completed_quality_missing = int(health.get("tushare_minute_latest_completed_quality_missing_count") or 0)
        minute_completed_quality_target_date = str(health.get("tushare_minute_latest_completed_quality_target_date") or "")
        minute_completed_quality_action = str(health.get("tushare_minute_latest_completed_quality_suggested_next_action") or "")
        wait_reason = str(health.get("extended_data_operator_wait_reason") or "")
        manual_review_required = _bool_value(health.get("extended_data_manual_review_required"), False)
        manual_review_reason = str(health.get("extended_data_manual_review_reason") or "")
        requires_confirmation = _bool_value(health.get("extended_data_requires_user_confirmation"), False)
        confirmation_command = str(health.get("extended_data_confirmation_required_command") or "")
        safe_next_commands = [str(item) for item in _as_list(health.get("extended_data_safe_next_commands")) if str(item).strip()]
        blocked_without_confirmation = [
            str(item) for item in _as_list(health.get("extended_data_blocked_without_confirmation")) if str(item).strip()
        ]
        import_domain_counts = {
            str(key): int(value or 0)
            for key, value in _as_dict(health.get("extended_import_domain_counts")).items()
            if str(key).strip()
        }
        missing_planned_domains = [
            str(item) for item in _as_list(health.get("extended_import_missing_planned_domains")) if str(item).strip()
        ]
        would_improve_domains = [
            str(item) for item in _as_list(health.get("extended_import_would_improve_domains")) if str(item).strip()
        ]
        import_max_files_per_rule = int(health.get("extended_import_max_files_per_rule") or 0)
        import_source_scan_limit_applied = _bool_value(health.get("extended_import_source_scan_limit_applied"), False)
        import_source_scan_limited_domains = [
            str(item) for item in _as_list(health.get("extended_import_source_scan_limited_domains")) if str(item).strip()
        ]
        import_full_audit_command = str(health.get("extended_import_full_audit_command") or "")
        import_source_scan_full_audit_command = str(health.get("extended_import_source_scan_full_audit_command") or "")
        summary.update({
            "missing_daily_count": missing,
            "stale_count": stale,
            "ready_count": ready,
            "extended_data_current_gate": current_gate,
            "extended_data_gate_suggested_next_action": gate_action,
            "extended_data_operator_wait_reason": wait_reason,
            "extended_data_manual_review_required": manual_review_required,
            "extended_data_manual_review_reason": manual_review_reason,
            "extended_data_requires_user_confirmation": requires_confirmation,
            "extended_data_confirmation_required_command": confirmation_command,
            "extended_data_safe_next_commands": safe_next_commands,
            "extended_data_blocked_without_confirmation": blocked_without_confirmation,
            "extended_import_domain_counts": import_domain_counts,
            "extended_import_missing_planned_domains": missing_planned_domains,
            "extended_import_duplicate_destination_count": int(health.get("extended_import_duplicate_destination_count") or 0),
            "extended_import_existing_destination_count": int(health.get("extended_import_existing_destination_count") or 0),
            "extended_import_would_improve_domains": would_improve_domains,
            "extended_import_max_files_per_rule": import_max_files_per_rule,
            "extended_import_source_scan_limit_applied": import_source_scan_limit_applied,
            "extended_import_source_scan_limited_domains": import_source_scan_limited_domains,
            "extended_import_full_audit_command": import_full_audit_command,
            "extended_import_source_scan_full_audit_command": import_source_scan_full_audit_command,
            "extended_data_queue_ok_count": queue_ok_count,
            "extended_data_queue_fail_count": queue_fail_count,
            "extended_data_queue_traceback_count": queue_traceback_count,
            "extended_data_queue_stdout_age_seconds": queue_stdout_age,
            "extended_data_queue_stderr_age_seconds": queue_stderr_age,
            "extended_data_queue_liveness": queue_liveness,
            "extended_data_queue_progress_state": queue_progress_state,
            "extended_data_queue_stage_progress": queue_stage_progress,
            "extended_data_queue_stage_requested_count": queue_stage_requested_count,
            "extended_data_queue_stage_completed_count": queue_stage_completed_count,
            "extended_data_queue_stage_remaining_count": queue_stage_remaining_count,
            "extended_data_queue_stage_remaining_keys": queue_stage_remaining_keys,
            "extended_data_queue_stage_current_elapsed_seconds": queue_stage_current_elapsed,
            "extended_data_queue_stage_last_event_age_seconds": queue_stage_last_event_age,
            "extended_data_queue_stage_last_event_status": queue_stage_last_event_status,
            "extended_data_queue_ok_delta": queue_ok_delta,
            "extended_data_queue_fail_delta": queue_fail_delta,
            "extended_data_queue_traceback_delta": queue_traceback_delta,
            "tushare_minute_status": minute_status,
            "tushare_minute_symbol_done_count": minute_done_count,
            "tushare_minute_symbol_count": minute_symbol_count,
            "tushare_minute_last_symbol": minute_last_symbol,
            "tushare_minute_traceback_count": minute_traceback_count,
            "tushare_minute_suggested_next_action": minute_status_action,
            "tushare_minute_coverage_status": minute_coverage_status,
            "tushare_minute_target_symbol_count": minute_target_symbol_count,
            "tushare_minute_current_available_count": minute_current_available_count,
            "tushare_minute_current_available_count_source": minute_current_available_count_source,
            "tushare_minute_canonical_current_available_count": minute_canonical_current_available_count,
            "tushare_minute_staged_current_not_imported_count": minute_staged_current_not_imported_count,
            "tushare_minute_canonical_current_available_ratio": minute_canonical_current_available_ratio,
            "tushare_minute_staged_current_not_imported_ratio": minute_staged_current_not_imported_ratio,
            "tushare_minute_missing_or_stale_count": minute_missing_or_stale_count,
            "tushare_minute_current_available_ratio": minute_current_available_ratio,
            "tushare_minute_coverage_suggested_next_action": minute_coverage_action,
            "tushare_minute_canonical_file_count": minute_canonical_file_count,
            "tushare_minute_latest_completed_job_path": minute_completed_job_path,
            "tushare_minute_latest_completed_job_symbol_count": minute_completed_job_symbol_count,
            "tushare_minute_latest_completed_job_canonical_current_count": minute_completed_job_current_count,
            "tushare_minute_latest_completed_job_missing_or_stale_count": minute_completed_job_missing_count,
            "tushare_minute_latest_completed_quality_status": minute_completed_quality_status,
            "tushare_minute_latest_completed_quality_total_symbols": minute_completed_quality_total,
            "tushare_minute_latest_completed_quality_usable_count": minute_completed_quality_usable,
            "tushare_minute_latest_completed_quality_invalid_count": minute_completed_quality_invalid,
            "tushare_minute_latest_completed_quality_missing_count": minute_completed_quality_missing,
            "tushare_minute_latest_completed_quality_target_date": minute_completed_quality_target_date,
            "tushare_minute_latest_completed_quality_suggested_next_action": minute_completed_quality_action,
            "extended_import_preapply_gate": health.get("extended_import_preapply_gate") or "",
            "extended_import_gate": health.get("extended_import_gate") or "",
            "tushare_retry_gate": health.get("tushare_retry_gate") or "",
        })
        summary["key_counts"].update({
            "missing_daily_count": missing,
            "stale_count": stale,
            "ready_count": ready,
            "intraday_missing_count": int(_as_dict(payload.get("intraday")).get("missing_count") or 0),
            "extended_data_current_gate": current_gate,
            "extended_data_queue_ok_count": queue_ok_count,
            "extended_data_queue_fail_count": queue_fail_count,
            "extended_data_queue_traceback_count": queue_traceback_count,
            "extended_data_queue_stdout_age_seconds": queue_stdout_age,
            "extended_data_queue_stderr_age_seconds": queue_stderr_age,
            "extended_data_queue_liveness": queue_liveness,
            "extended_data_queue_progress_state": queue_progress_state,
            "extended_data_queue_stage_progress": queue_stage_progress,
            "extended_data_queue_stage_requested_count": queue_stage_requested_count,
            "extended_data_queue_stage_completed_count": queue_stage_completed_count,
            "extended_data_queue_stage_remaining_count": queue_stage_remaining_count,
            "extended_data_queue_stage_remaining_keys": queue_stage_remaining_keys,
            "extended_data_queue_stage_current_elapsed_seconds": queue_stage_current_elapsed,
            "extended_data_queue_stage_last_event_age_seconds": queue_stage_last_event_age,
            "extended_data_queue_stage_last_event_status": queue_stage_last_event_status,
            "extended_data_queue_ok_delta": queue_ok_delta,
            "extended_data_queue_fail_delta": queue_fail_delta,
            "extended_data_queue_traceback_delta": queue_traceback_delta,
            "tushare_minute_symbol_done_count": minute_done_count,
            "tushare_minute_symbol_count": minute_symbol_count,
            "tushare_minute_last_symbol": minute_last_symbol,
            "tushare_minute_target_symbol_count": minute_target_symbol_count,
            "tushare_minute_current_available_count": minute_current_available_count,
            "tushare_minute_current_available_count_source": minute_current_available_count_source,
            "tushare_minute_canonical_current_available_count": minute_canonical_current_available_count,
            "tushare_minute_staged_current_not_imported_count": minute_staged_current_not_imported_count,
            "tushare_minute_canonical_current_available_ratio": minute_canonical_current_available_ratio,
            "tushare_minute_staged_current_not_imported_ratio": minute_staged_current_not_imported_ratio,
            "tushare_minute_missing_or_stale_count": minute_missing_or_stale_count,
            "tushare_minute_current_available_ratio": minute_current_available_ratio,
            "tushare_minute_canonical_file_count": minute_canonical_file_count,
            "tushare_minute_latest_completed_job_symbol_count": minute_completed_job_symbol_count,
            "tushare_minute_latest_completed_job_canonical_current_count": minute_completed_job_current_count,
            "tushare_minute_latest_completed_job_missing_or_stale_count": minute_completed_job_missing_count,
            "tushare_minute_latest_completed_quality_status": minute_completed_quality_status,
            "tushare_minute_latest_completed_quality_total_symbols": minute_completed_quality_total,
            "tushare_minute_latest_completed_quality_usable_count": minute_completed_quality_usable,
            "tushare_minute_latest_completed_quality_invalid_count": minute_completed_quality_invalid,
            "tushare_minute_latest_completed_quality_missing_count": minute_completed_quality_missing,
            "tushare_minute_latest_completed_quality_target_date": minute_completed_quality_target_date,
            "extended_data_operator_wait_reason": wait_reason,
            "extended_data_manual_review_required": manual_review_required,
            "extended_data_manual_review_reason": manual_review_reason,
            "extended_data_requires_user_confirmation": requires_confirmation,
            "extended_data_safe_next_commands": safe_next_commands[:3],
            "extended_import_domain_counts": import_domain_counts,
            "extended_import_missing_planned_domains": missing_planned_domains,
            "extended_import_source_scan_limit_applied": import_source_scan_limit_applied,
            "extended_import_source_scan_limited_domains": import_source_scan_limited_domains,
            "extended_import_full_audit_command": import_full_audit_command,
            "extended_import_source_scan_full_audit_command": import_source_scan_full_audit_command,
        })

    elif task == "backtest":
        metrics = _as_dict(payload.get("metrics"))
        symbols = _as_list(payload.get("symbols"))
        summary.update({
            "mode": payload.get("mode") or "",
            "strategy": payload.get("strategy") or "",
            "symbols_count": len(symbols),
            "trade_count": int(payload.get("trade_count") or len(_as_list(payload.get("trades")))),
            "metrics_preview": _metrics_preview(metrics),
        })
        summary["key_counts"].update({
            "symbols_count": summary["symbols_count"],
            "trade_count": summary["trade_count"],
        })

    elif task == "screen":
        selected = int(payload.get("total_matched") or 0)
        summary.update({
            "selected_count": selected,
            "warning_kind": payload.get("warning_kind") or "",
        })
        summary["key_counts"].update({
            "selected_count": selected,
            "total_scanned": int(payload.get("total_scanned") or 0),
        })

    elif task == "market_intelligence":
        intel = _as_dict(payload.get("market_intelligence_summary"))
        candidate_count = int(intel.get("candidate_count") or payload.get("candidate_count") or 0)
        theme_count = int(intel.get("hot_sector_count") or payload.get("theme_count") or 0)
        board_watch_count = int(intel.get("board_watch_count") or payload.get("board_watch_count") or 0)
        warning_blob = " ".join(warnings).lower()
        summary.update({
            "candidate_count": candidate_count,
            "theme_count": theme_count,
            "board_watch_count": board_watch_count,
            "news_skipped_offline": ("news_skipped_offline" in warning_blob) or (not _bool_value(payload.get("network"), False)),
        })
        summary["key_counts"].update({
            "candidate_count": candidate_count,
            "theme_count": theme_count,
            "board_watch_count": board_watch_count,
        })

    elif task == "walk_forward":
        walk = _as_dict(payload.get("walk_forward_summary"))
        quality = _as_dict(payload.get("backtest_quality"))
        window_count = int(walk.get("total_windows") or walk.get("window_count") or 0)
        trade_count = int(walk.get("total_trades") or 0)
        summary.update({
            "window_count": window_count,
            "trade_count": trade_count,
            "quality_status": quality.get("quality_grade") or payload.get("quality_status") or "",
        })
        summary["key_counts"].update({"window_count": window_count, "trade_count": trade_count})

    elif task in {"daily_workflow", "daily"}:
        guardrails = _as_dict(payload.get("guardrails") or payload.get("guardrail_summary"))
        research = _as_dict(payload.get("research_dashboard_snapshot"))
        multi_book = _as_dict(payload.get("multi_strategy_account_book_snapshot"))
        intraday_live = _as_dict(payload.get("intraday_live_snapshot"))
        risk_attribution = _as_dict(payload.get("risk_attribution_snapshot"))
        strategy_recommendation = _as_dict(payload.get("strategy_recommendation_snapshot"))
        risk_alert_snapshot = _as_dict(payload.get("risk_alert_snapshot"))
        experiments = _as_dict(payload.get("experiment_tracker_snapshot"))
        outputs = _output_paths(payload)
        summary.update({
            "generated_reports_count": _count(outputs),
            "guardrail_status": guardrails.get("status") or payload.get("guardrail_status") or "",
            "research_dashboard_status": research.get("status") or "",
            "research_readiness_level": research.get("readiness_level") or "",
            "research_warning_count": int(research.get("warning_count") or 0),
            "research_dashboard_report": research.get("latest_report") or "",
            "pit_universe": _bool_value(research.get("pit_universe"), False),
            "pit_status": research.get("pit_status") or "",
            "multi_strategy_book_status": multi_book.get("status") or "",
            "multi_strategy_book_portfolio": multi_book.get("portfolio_name") or "",
            "multi_strategy_book_sub_account_count": int(multi_book.get("sub_account_count") or 0),
            "multi_strategy_book_report": multi_book.get("latest_report") or "",
            "intraday_live_status": intraday_live.get("status") or "",
            "intraday_live_snapshot_time": intraday_live.get("snapshot_time") or "",
            "intraday_live_report": intraday_live.get("latest_report") or "",
            "intraday_live_warning_count": len(_as_list(intraday_live.get("warnings"))),
            "risk_attribution_status": risk_attribution.get("status") or "",
            "risk_attribution_portfolio": risk_attribution.get("portfolio_name") or "",
            "risk_attribution_report": risk_attribution.get("latest_report") or "",
            "risk_attribution_warning_count": len(_as_list(risk_attribution.get("warnings"))),
            "strategy_recommendation_status": strategy_recommendation.get("status") or "",
            "strategy_recommendation_count": int(strategy_recommendation.get("recommendation_count") or 0),
            "strategy_recommendation_report": strategy_recommendation.get("latest_report") or "",
            "strategy_recommendation_warning_count": len(_as_list(strategy_recommendation.get("warnings"))),
            "risk_alert_status": risk_alert_snapshot.get("status") or "",
            "risk_alert_count": int(risk_alert_snapshot.get("alert_count") or 0),
            "risk_alert_critical_count": int(risk_alert_snapshot.get("critical_count") or 0),
            "smart_stop_triggered_count": int(_as_dict(risk_alert_snapshot.get("smart_stop_summary")).get("triggered_count") or 0),
            "risk_alert_report": risk_alert_snapshot.get("latest_report") or "",
            "experiment_tracker_status": experiments.get("status") or "",
            "experiment_tracker_count": int(experiments.get("experiment_count") or 0),
            "experiment_tracker_report": experiments.get("latest_index") or "",
        })
        summary["key_counts"].update({"generated_reports_count": summary["generated_reports_count"]})

    elif task == "smart_stop":
        summary.update({
            "position_count": int(payload.get("position_count") or 0),
            "safe_count": int(payload.get("safe_count") or 0),
            "watch_count": int(payload.get("watch_count") or 0),
            "triggered_count": int(payload.get("triggered_count") or 0),
        })
        summary["key_counts"].update({"position_count": summary["position_count"]})

    elif task == "risk_alerts":
        notification_status = _as_dict(payload.get("notification_status"))
        summary.update({
            "position_count": int(payload.get("position_count") or 0),
            "alert_count": int(payload.get("alert_count") or 0),
            "critical_count": int(payload.get("critical_count") or 0),
            "top_alerts": _as_list(payload.get("alerts"))[:5],
            "notification_status": notification_status,
        })
        summary["key_counts"].update({"position_count": summary["position_count"], "alert_count": summary["alert_count"]})

    elif task == "similar_day":
        stats = _as_dict(payload.get("summary_stats"))
        similar_days = _as_list(payload.get("similar_days"))
        summary.update({
            "target_date": payload.get("target_date") or "",
            "similar_day_count": int(payload.get("similar_day_count") or len(similar_days)),
            "avg_forward_5d": stats.get("avg_forward_5d"),
            "top_similar_days": similar_days[:5],
        })
        summary["key_counts"].update({"similar_day_count": summary["similar_day_count"]})

    elif task in {"stress_scenarios", "stress_test"}:
        worst = _as_dict(payload.get("worst_scenario"))
        scenarios = _as_list(payload.get("scenarios"))
        summary.update({
            "scenario_count": int(payload.get("scenario_count") or len(scenarios)),
            "worst_scenario_id": worst.get("scenario_id") or "",
            "worst_estimated_return": worst.get("estimated_portfolio_return"),
            "position_count": int(payload.get("position_count") or 0),
        })
        summary["key_counts"].update({"scenario_count": summary["scenario_count"], "position_count": summary["position_count"]})

    elif task == "style_attribution":
        coverage = _as_dict(payload.get("coverage"))
        summary.update({
            "position_count": int(payload.get("position_count") or 0),
            "exposure_coverage_ratio": coverage.get("exposure_coverage_ratio"),
            "top_style_tilts": _as_list(payload.get("dominant_style_tilts"))[:5],
            "top_style_contributions": _as_list(payload.get("style_contribution_ranking"))[:5],
        })
        summary["key_counts"].update({"position_count": summary["position_count"]})

    elif task == "decision_journal":
        stats = _as_dict(payload.get("statistics"))
        summary.update({
            "total_decisions": int(stats.get("total_decisions") or payload.get("total_decisions") or 0),
            "reviewed_count": int(stats.get("reviewed_count") or payload.get("reviewed_count") or 0),
            "pending_count": int(stats.get("pending_count") or payload.get("pending_count") or 0),
            "hit_rate": stats.get("hit_rate") if "hit_rate" in stats else payload.get("hit_rate"),
            "latest_entries": _as_list(payload.get("items"))[:5],
        })
        summary["key_counts"].update({"decision_count": summary["total_decisions"]})

    elif task == "notification_center":
        summary.update({
            "message_count": int(payload.get("message_count") or 0),
            "sent_count": int(payload.get("sent_count") or 0),
            "skipped_dedupe_count": int(payload.get("skipped_dedupe_count") or 0),
            "dry_run": _bool_value(payload.get("dry_run"), True),
            "channels_attempted": _as_list(payload.get("channels_attempted")),
        })
        summary["key_counts"].update({"message_count": summary["message_count"], "sent_count": summary["sent_count"]})

    elif task == "intraday_live_workbench":
        partial = _as_dict(payload.get("partial_daily"))
        condition = _as_dict(payload.get("condition_plan_status"))
        sector = _as_dict(payload.get("sector_heat"))
        risks = _as_list(payload.get("risk_flags"))
        summary.update({
            "trade_date": payload.get("trade_date") or "",
            "snapshot_time": payload.get("snapshot_time") or "",
            "market_session": payload.get("market_session") or "unknown",
            "watchlist_count": int(payload.get("watchlist_count") or 0),
            "quote_count": int(payload.get("quote_count") or 0),
            "partial_daily_row_count": int(partial.get("row_count") or 0),
            "intraday_score_count": len(_as_list(payload.get("intraday_scores"))),
            "triggered_condition_count": int(condition.get("triggered_count") or 0),
            "sector_heat_count": len(_as_list(sector.get("items"))),
            "live_account_initialized": _bool_value(payload.get("live_account_initialized"), False),
            "risk_flags_top": [str(item) for item in risks[:5]],
        })
        summary["target_date"] = summary["trade_date"]
        summary["key_counts"].update({
            "watchlist_count": summary["watchlist_count"],
            "quote_count": summary["quote_count"],
            "partial_daily_row_count": summary["partial_daily_row_count"],
            "intraday_score_count": summary["intraday_score_count"],
        })

    elif task == "intraday_live_account_init":
        summary.update({
            "path": payload.get("path") or "",
            "created": _bool_value(payload.get("created"), False),
            "overwritten": _bool_value(payload.get("overwritten"), False),
            "live_account_initialized": _bool_value(payload.get("live_account_initialized"), False),
        })
        summary["key_counts"].update({"position_count": len(_as_list(_as_dict(payload.get("account_state")).get("positions")))})

    elif task == "multi_strategy_account_book":
        risks = _as_list(payload.get("risk_flags"))
        summary.update({
            "portfolio_name": payload.get("portfolio_name") or "",
            "sub_account_count": int(payload.get("sub_account_count") or 0),
            "initial_cash": payload.get("initial_cash") or 0,
            "equity": payload.get("equity") or 0,
            "risk_flags_top": [str(item) for item in risks[:5]],
        })
        summary["key_counts"].update({
            "sub_account_count": summary["sub_account_count"],
            "position_count": sum(len(_as_list(_as_dict(row).get("positions"))) for row in _as_list(payload.get("sub_accounts"))),
        })

    elif task == "risk_attribution":
        brinson = _as_dict(payload.get("brinson"))
        factor = _as_dict(payload.get("factor_attribution"))
        risks = _as_list(payload.get("risk_flags"))
        summary.update({
            "attribution_mode": payload.get("attribution_mode") or "",
            "portfolio_return": payload.get("portfolio_return"),
            "benchmark_return": payload.get("benchmark_return"),
            "excess_return": payload.get("excess_return"),
            "allocation_effect": brinson.get("allocation_effect"),
            "selection_effect": brinson.get("selection_effect"),
            "interaction_effect": brinson.get("interaction_effect"),
            "factor_count": int(factor.get("factor_count") or 0),
            "risk_flags_top": [str(item) for item in risks[:5]],
        })
        summary["key_counts"].update({
            "industry_count": len(_as_list(brinson.get("by_industry"))),
            "factor_count": summary["factor_count"],
        })

    elif task == "strategy_recommendation":
        recs = _as_list(payload.get("recommendations"))
        risks = _as_list(payload.get("risk_flags"))
        strategy_counts = _as_dict(payload.get("strategy_counts"))
        summary.update({
            "mode": payload.get("mode") or "",
            "recommendation_count": int(payload.get("recommendation_count") or len(recs)),
            "top_recommendations": recs[:5],
            "strategy_counts": strategy_counts,
            "top_sectors": _as_list(payload.get("top_sectors"))[:5],
            "risk_flags_top": [str(item) for item in risks[:5]],
        })
        summary["key_counts"].update({
            "recommendation_count": summary["recommendation_count"],
            "strategy_type_count": len([key for key, value in strategy_counts.items() if value]),
        })

    elif task in {"risk_attribution_lite", "risk_observation_lite"}:
        sub = _as_dict(payload.get("sub_account_attribution"))
        sector = _as_dict(payload.get("sector_exposure"))
        factor = _as_dict(payload.get("factor_support_attribution"))
        data = _as_dict(payload.get("data_risk_attribution"))
        intraday = _as_dict(payload.get("intraday_context_attribution"))
        risks = _as_list(payload.get("risk_flags"))
        contributors = _as_list(sub.get("contributions"))
        sectors = _as_list(sector.get("sector_exposure"))
        summary.update({
            "mode": payload.get("mode") or "observation_lite",
            "compatibility_alias": _bool_value(payload.get("compatibility_alias"), False),
            "portfolio_name": payload.get("portfolio_name") or "",
            "input_status": _as_dict(payload.get("input_status")),
            "sub_account_count": int(sub.get("sub_account_count") or 0),
            "top_contributors": contributors[:5],
            "top_sector_exposures": sectors[:5],
            "usable_factor_count": int(factor.get("usable_factor_count") or 0),
            "weak_factor_count": int(factor.get("weak_factor_count") or 0),
            "pit_universe": _bool_value(data.get("pit_universe"), False),
            "intraday_context_available": _bool_value(intraday.get("available"), False),
            "risk_flags_top": [str(item) for item in risks[:5]],
        })
        summary["key_counts"].update({
            "sub_account_count": summary["sub_account_count"],
            "sector_exposure_count": len(sectors),
            "risk_flag_count": len(risks),
        })

    elif task == "experiments":
        rows = _as_list(payload.get("latest_experiments"))
        diff = _as_dict(payload.get("diff"))
        summary.update({
            "experiment_count": int(payload.get("experiment_count") or len(rows)),
            "latest_experiments": rows[:5],
            "filters": _as_dict(payload.get("filters")),
            "diff": diff,
        })
        summary["key_counts"].update({
            "experiment_count": summary["experiment_count"],
            "diff_available": 1 if diff.get("available") else 0,
        })

    elif task == "data_bootstrap":
        summary.update({
            "daily_ready": _bool_value(payload.get("daily_ready"), False),
            "bootstrap_mode": payload.get("bootstrap_mode") or "",
            "updated_daily_count": int(payload.get("updated_daily_count") or 0),
            "symbol_count": int(payload.get("symbol_count") or len(_as_list(payload.get("symbols")))),
        })
        summary["key_counts"].update({
            "updated_daily_count": summary["updated_daily_count"],
            "symbol_count": summary["symbol_count"],
        })

    elif task == "factor_evaluation":
        metrics = _as_dict(payload.get("factor_metrics"))
        universe = _as_dict(payload.get("universe"))
        statuses = [str(_as_dict(metric).get("health_status") or "") for metric in metrics.values()]
        usable = sum(1 for item in statuses if item in {"strong", "usable"})
        weak = sum(1 for item in statuses if item == "weak")
        invalid = sum(1 for item in statuses if item in {"invalid", "insufficient_data"})
        ranked = sorted(
            (
                {
                    "factor": name,
                    "rank_ic_mean": _as_dict(metric).get("rank_ic_mean") or 0,
                    "health_status": _as_dict(metric).get("health_status") or "",
                }
                for name, metric in metrics.items()
            ),
            key=lambda item: abs(float(item.get("rank_ic_mean") or 0)),
            reverse=True,
        )
        weakest = sorted(
            (
                {
                    "factor": name,
                    "rank_ic_mean": _as_dict(metric).get("rank_ic_mean") or 0,
                    "health_status": _as_dict(metric).get("health_status") or "",
                }
                for name, metric in metrics.items()
            ),
            key=lambda item: float(item.get("rank_ic_mean") or 0),
        )
        summary.update({
            "best_factors": ranked[:3],
            "weakest_factors": weakest[:3],
            "top_rank_ic_factors": ranked[:5],
            "pit_universe": _bool_value(universe.get("pit_universe"), False),
            "data_status": payload.get("data_source") or "",
        })
        summary["key_counts"].update({
            "factor_count": len(metrics),
            "symbol_count": int(universe.get("symbol_count") or 0),
            "date_count": int(universe.get("date_count") or 0),
            "sample_count": int(universe.get("sample_count") or 0),
            "usable_factor_count": usable,
            "weak_factor_count": weak,
            "invalid_factor_count": invalid,
        })

    elif task == "factor_combination":
        universe = _as_dict(payload.get("universe"))
        score_summary = _as_dict(payload.get("score_summary"))
        config = _as_dict(payload.get("config"))
        top_candidates = _as_list(payload.get("top_candidates"))
        neutralization = _as_dict(payload.get("neutralization"))
        summary.update({
            "weight_method": config.get("weight_method") or "",
            "standardize_method": config.get("standardize_method") or "",
            "factor_names": _as_list(payload.get("factor_names")),
            "raw_weights": _as_dict(payload.get("raw_weights")),
            "normalized_weights": _as_dict(payload.get("normalized_weights") or payload.get("factor_weights")),
            "cross_section_count": int(payload.get("cross_section_count") or score_summary.get("latest_symbol_count") or 0),
            "insufficient_cross_section": _bool_value(payload.get("insufficient_cross_section"), False),
            "skipped_symbols": _as_list(payload.get("skipped_symbols")),
            "factor_coverage": _as_dict(payload.get("factor_coverage")),
            "score_distribution": _as_dict(payload.get("score_distribution")),
            "top_symbols": _as_list(payload.get("top_symbols")),
            "neutralization_status": {
                "industry": neutralization.get("industry_status") or "",
                "market_cap": neutralization.get("market_cap_status") or "",
            },
            "top_candidates_preview": top_candidates[:3],
            "data_status": payload.get("data_source") or "",
            "pit_universe": _bool_value(universe.get("pit_universe"), False),
        })
        summary["key_counts"].update({
            "factor_count": len(_as_list(config.get("factors"))),
            "symbol_count": int(universe.get("symbol_count") or 0),
            "date_count": int(universe.get("date_count") or 0),
            "sample_count": int(universe.get("sample_count") or 0),
            "scored_count": int(score_summary.get("score_count") or 0),
            "top_candidate_count": len(top_candidates),
        })

    elif task == "combined_score_backtest":
        universe = _as_dict(payload.get("universe"))
        performance = _as_dict(payload.get("performance"))
        portfolio_summary = _as_dict(payload.get("portfolio_summary"))
        config = _as_dict(payload.get("config"))
        selected = _as_list(portfolio_summary.get("latest_selected_symbols"))
        summary.update({
            "portfolio_mode": config.get("portfolio_mode") or "",
            "profile": payload.get("profile") or config.get("profile") or "",
            "initial_cash": payload.get("initial_cash") or config.get("initial_cash") or 0,
            "max_positions": payload.get("max_positions") or 0,
            "max_position_pct": payload.get("max_position_pct") or config.get("max_position_pct") or 0,
            "top_n": int(config.get("top_n") or 0),
            "lot_size": payload.get("lot_size") or config.get("lot_size") or 0,
            "price_filter_enabled": payload.get("price_filter_enabled") or config.get("price_filter_enabled") or False,
            "price_filter_max_price": payload.get("price_filter_max_price") or config.get("price_filter_max_price") or 0,
            "price_filter_evaluated_on": payload.get("price_filter_evaluated_on") or config.get("price_filter_evaluated_on") or "",
            "quantile": config.get("quantile") or 0,
            "long_short": _bool_value(config.get("long_short"), False),
            "rebalance_frequency": config.get("rebalance_frequency") or "",
            "net_return": performance.get("net_return") or 0,
            "annualized_return": performance.get("annualized_return") or 0,
            "sharpe_ratio": performance.get("sharpe_ratio") or 0,
            "max_drawdown": performance.get("max_drawdown") or 0,
            "turnover_mean": performance.get("turnover_mean") or 0,
            "latest_selected_preview": selected[:5],
            "data_status": payload.get("data_source") or "",
            "pit_universe": _bool_value(universe.get("pit_universe"), False),
            "pit_mode": payload.get("pit_mode") or "",
            "survivorship_bias_risk": payload.get("survivorship_bias_risk") or "",
            "benchmark": _as_dict(payload.get("benchmark")),
        })
        summary["key_counts"].update({
            "symbol_count": int(universe.get("symbol_count") or 0),
            "date_count": int(universe.get("date_count") or 0),
            "signal_date_count": int(universe.get("signal_date_count") or 0),
            "rebalance_count": int(performance.get("rebalance_count") or 0),
            "latest_selected_count": int(portfolio_summary.get("latest_selected_count") or 0),
        })

    elif task == "sector_rotation":
        universe = _as_dict(payload.get("universe"))
        benchmark = _as_dict(payload.get("benchmark"))
        rankings = _as_list(payload.get("sector_rankings"))
        improving = _as_list(payload.get("top_improving_sectors"))
        weakening = _as_list(payload.get("weakening_sectors"))
        hot_count = sum(1 for item in rankings if _as_dict(item).get("heat_status") == "hot")
        improving_count = sum(1 for item in rankings if _as_dict(item).get("heat_status") in {"hot", "improving"})
        weakening_count = sum(1 for item in rankings if _as_dict(item).get("heat_status") in {"weakening", "cold"})
        summary.update({
            "benchmark_method": benchmark.get("method") or _as_dict(payload.get("config")).get("benchmark_method") or "",
            "top_sectors_preview": rankings[:3],
            "top_improving_preview": improving[:3],
            "weakening_preview": weakening[:3],
            "data_status": payload.get("data_source") or "",
            "pit_universe": _bool_value(universe.get("pit_universe"), False),
        })
        summary["key_counts"].update({
            "symbol_count": int(universe.get("symbol_count") or 0),
            "sector_count": int(universe.get("sector_count") or 0),
            "date_count": int(universe.get("date_count") or 0),
            "sample_count": int(universe.get("sample_count") or 0),
            "hot_sector_count": hot_count,
            "improving_sector_count": improving_count,
            "weakening_sector_count": weakening_count,
        })

    elif task == "factor_research_dashboard":
        module_health = _as_dict(payload.get("module_health"))
        dashboard = _as_dict(payload.get("dashboard_summary"))
        factor = _as_dict(payload.get("factor_health_summary"))
        backtest = _as_dict(payload.get("backtest_summary"))
        sector = _as_dict(payload.get("sector_rotation_summary"))
        pit_status = _as_dict(payload.get("pit_status"))
        available = sum(1 for item in module_health.values() if _as_dict(item).get("available"))
        missing = len(_as_list(dashboard.get("missing_modules")))
        stale = len(_as_list(dashboard.get("stale_modules")))
        summary.update({
            "readiness_level": dashboard.get("readiness_level") or "",
            "research_status": dashboard.get("research_status") or "",
            "module_status_preview": [
                {"module": name, "status": _as_dict(item).get("status") or "missing"}
                for name, item in list(module_health.items())[:4]
            ],
            "key_findings_preview": _as_list(dashboard.get("key_findings"))[:3],
            "risk_flags_preview": _as_list(dashboard.get("risk_flags"))[:3],
            "suggested_next_actions_preview": _as_list(dashboard.get("suggested_next_actions"))[:3],
            "backtest_sharpe_ratio": backtest.get("sharpe_ratio") or 0,
            "backtest_max_drawdown": backtest.get("max_drawdown") or 0,
            "pit_universe": _bool_value(pit_status.get("pit_universe"), False),
        })
        summary["key_counts"].update({
            "available_module_count": available,
            "missing_module_count": missing,
            "stale_module_count": stale,
            "strong_factor_count": int(factor.get("strong_count") or 0),
            "usable_factor_count": int(factor.get("usable_count") or 0),
            "weak_factor_count": int(factor.get("weak_count") or 0),
            "hot_sector_count": int(sector.get("hot_sector_count") or 0),
            "improving_sector_count": int(sector.get("improving_sector_count") or 0),
        })

    elif task == "point_in_time_universe":
        pit_status = _as_dict(payload.get("pit_status"))
        excluded = _as_dict(payload.get("excluded_counts"))
        excluded_count = sum(int(value or 0) for value in excluded.values())
        summary.update({
            "pit_universe": _bool_value(pit_status.get("pit_universe"), False),
            "listings_available": _bool_value(pit_status.get("available"), False),
            "list_date_coverage": pit_status.get("list_date_coverage") or 0.0,
            "delist_date_coverage": pit_status.get("delist_date_coverage") or 0.0,
            "st_coverage": pit_status.get("st_coverage") or 0.0,
            "suspend_coverage": pit_status.get("suspend_coverage") or 0.0,
        })
        summary["key_counts"].update({
            "symbol_count": int(payload.get("symbol_count") or pit_status.get("symbol_count") or 0),
            "excluded_count": excluded_count,
            "invalid_date_count": int(excluded.get("invalid_dates") or 0),
        })

    return summary


def format_human_summary(summary: Mapping[str, Any]) -> str:
    paths = _as_dict(summary.get("output_paths"))
    warnings_top = [str(item) for item in _as_list(summary.get("warnings_top"))]
    if summary.get("task_type") == "factor_evaluation":
        counts = _as_dict(summary.get("key_counts"))
        best = _as_list(summary.get("top_rank_ic_factors"))
        best_line = "-"
        if best and isinstance(best[0], Mapping):
            best_line = f"{best[0].get('factor')} = {best[0].get('rank_ic_mean')}"
        lines = [
            "QuantDesk factor-eval summary",
            f"Status: {summary.get('status') or 'unknown'}",
            (
                "Factors: "
                f"{counts.get('factor_count', 0)} evaluated, "
                f"{counts.get('usable_factor_count', 0)} usable, "
                f"{counts.get('weak_factor_count', 0)} weak, "
                f"{counts.get('invalid_factor_count', 0)} insufficient"
            ),
            f"Universe: {counts.get('symbol_count', 0)} symbols, {counts.get('date_count', 0)} dates",
            f"Best Rank IC: {best_line}",
            f"Warnings: {summary.get('warning_count', 0)}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if summary.get("suggested_next_action"):
            lines.append(f"Next: {summary.get('suggested_next_action')}")
        if paths:
            lines.append(f"Full JSON: {paths.get('latest_json') or paths.get('json') or next(iter(paths.values()))}")
        if paths.get("report") or paths.get("latest_report"):
            lines.append(f"Report: {paths.get('latest_report') or paths.get('report')}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") == "factor_combination":
        counts = _as_dict(summary.get("key_counts"))
        neutral = _as_dict(summary.get("neutralization_status"))
        top = _as_list(summary.get("top_candidates_preview"))
        top_line = "-"
        if top and isinstance(top[0], Mapping):
            top_line = f"{top[0].get('symbol')} score={top[0].get('combined_score')}"
        lines = [
            "QuantDesk factor-combine summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"Factors: {counts.get('factor_count', 0)}",
            f"Weight method: {summary.get('weight_method') or '-'}",
            f"Standardize: {summary.get('standardize_method') or '-'}",
            f"Neutralization: industry={neutral.get('industry') or '-'}, market_cap={neutral.get('market_cap') or '-'}",
            f"Scores: {counts.get('symbol_count', 0)} symbols on {summary.get('target_date') or '-'}",
            f"Top: {top_line}",
            f"Warnings: {summary.get('warning_count', 0)}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if summary.get("suggested_next_action"):
            lines.append(f"Next: {summary.get('suggested_next_action')}")
        if paths:
            lines.append(f"Full JSON: {paths.get('latest_json') or paths.get('json') or next(iter(paths.values()))}")
        if paths.get("report") or paths.get("latest_report"):
            lines.append(f"Report: {paths.get('latest_report') or paths.get('report')}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") == "combined_score_backtest":
        counts = _as_dict(summary.get("key_counts"))
        latest = _as_list(summary.get("latest_selected_preview"))
        latest_line = ", ".join(str(item) for item in latest[:3]) if latest else "-"
        lines = [
            "QuantDesk combined-score-backtest summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"Mode: {summary.get('portfolio_mode') or '-'}, top_n={summary.get('top_n')}, rebalance={summary.get('rebalance_frequency') or '-'}",
            f"Signals: {counts.get('signal_date_count', 0)} dates, {counts.get('symbol_count', 0)} symbols",
            f"Net Return: {summary.get('net_return')}",
            f"Sharpe: {summary.get('sharpe_ratio')}",
            f"Max Drawdown: {summary.get('max_drawdown')}",
            f"Turnover Mean: {summary.get('turnover_mean')}",
            f"Latest selected: {latest_line}",
            f"Warnings: {summary.get('warning_count', 0)}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if summary.get("suggested_next_action"):
            lines.append(f"Next: {summary.get('suggested_next_action')}")
        if paths:
            lines.append(f"Full JSON: {paths.get('latest_json') or paths.get('json') or next(iter(paths.values()))}")
        if paths.get("report") or paths.get("latest_report"):
            lines.append(f"Report: {paths.get('latest_report') or paths.get('report')}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") == "sector_rotation":
        counts = _as_dict(summary.get("key_counts"))
        top = _as_list(summary.get("top_sectors_preview"))
        improving = _as_list(summary.get("top_improving_preview"))
        weakening = _as_list(summary.get("weakening_preview"))
        top_line = "-"
        if top and isinstance(top[0], Mapping):
            top_line = f"{top[0].get('sector_name')} heat={top[0].get('heat_score')}, rank={top[0].get('current_rank')}"
        improving_line = "-"
        if improving and isinstance(improving[0], Mapping):
            improving_line = f"{improving[0].get('sector_name')} rank {improving[0].get('rank_improvement_5d')}"
        weakening_line = "-"
        if weakening and isinstance(weakening[0], Mapping):
            weakening_line = f"{weakening[0].get('sector_name')} rank {weakening[0].get('rank_improvement_5d')}"
        lines = [
            "QuantDesk sector-rotation summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"Sectors: {counts.get('sector_count', 0)}",
            f"Benchmark: {summary.get('benchmark_method') or '-'}",
            f"Top: {top_line}",
            f"Improving: {improving_line}",
            f"Weakening: {weakening_line}",
            f"Warnings: {summary.get('warning_count', 0)}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if summary.get("suggested_next_action"):
            lines.append(f"Next: {summary.get('suggested_next_action')}")
        if paths:
            lines.append(f"Full JSON: {paths.get('latest_json') or paths.get('json') or next(iter(paths.values()))}")
        if paths.get("report") or paths.get("latest_report"):
            lines.append(f"Report: {paths.get('latest_report') or paths.get('report')}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") == "factor_research_dashboard":
        counts = _as_dict(summary.get("key_counts"))
        risks = _as_list(summary.get("risk_flags_preview"))
        actions = _as_list(summary.get("suggested_next_actions_preview"))
        risk_line = "; ".join(str(item) for item in risks[:2]) if risks else "-"
        next_line = str(actions[0]) if actions else summary.get("suggested_next_action") or "-"
        lines = [
            "QuantDesk research-dashboard summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"Readiness: {summary.get('readiness_level') or '-'}",
            (
                "Modules: "
                f"{counts.get('available_module_count', 0)} available, "
                f"{counts.get('missing_module_count', 0)} missing, "
                f"{counts.get('stale_module_count', 0)} stale"
            ),
            f"Factors: {counts.get('usable_factor_count', 0)} usable, {counts.get('weak_factor_count', 0)} weak",
            f"Backtest: Sharpe={summary.get('backtest_sharpe_ratio')}, MaxDD={summary.get('backtest_max_drawdown')}",
            f"Sectors: {counts.get('hot_sector_count', 0)} hot, {counts.get('improving_sector_count', 0)} improving",
            f"Risks: {risk_line}",
            f"Warnings: {summary.get('warning_count', 0)}",
            f"Next: {next_line}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if paths:
            lines.append(f"Full JSON: {paths.get('latest_json') or paths.get('json') or next(iter(paths.values()))}")
        if paths.get("report") or paths.get("latest_report"):
            lines.append(f"Report: {paths.get('latest_report') or paths.get('report')}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") == "point_in_time_universe":
        counts = _as_dict(summary.get("key_counts"))
        lines = [
            "QuantDesk pit-universe summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"PIT universe: {str(bool(summary.get('pit_universe'))).lower()}",
            f"Listings available: {str(bool(summary.get('listings_available'))).lower()}",
            f"Symbols: {counts.get('symbol_count', 0)}",
            f"Excluded: {counts.get('excluded_count', 0)}",
            f"List date coverage: {summary.get('list_date_coverage')}",
            f"Warnings: {summary.get('warning_count', 0)}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if summary.get("suggested_next_action"):
            lines.append(f"Next: {summary.get('suggested_next_action')}")
        if paths:
            lines.append(f"Full JSON: {paths.get('latest_json') or paths.get('json') or next(iter(paths.values()))}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") == "multi_strategy_account_book":
        counts = _as_dict(summary.get("key_counts"))
        risks = _as_list(summary.get("risk_flags_top"))
        risk_line = "; ".join(str(item) for item in risks[:3]) if risks else "-"
        lines = [
            "QuantDesk multi-strategy-book summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"Portfolio: {summary.get('portfolio_name') or '-'}",
            f"Sub-accounts: {summary.get('sub_account_count', 0)}",
            f"Initial cash: {summary.get('initial_cash')}",
            f"Equity: {summary.get('equity')}",
            f"Positions: {counts.get('position_count', 0)}",
            f"Risks: {risk_line}",
            f"Warnings: {summary.get('warning_count', 0)}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if paths:
            lines.append(f"Full JSON: {paths.get('latest_json') or paths.get('json') or next(iter(paths.values()))}")
        if paths.get("latest_report") or paths.get("report"):
            lines.append(f"Report: {paths.get('latest_report') or paths.get('report')}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") == "intraday_live_workbench":
        risks = _as_list(summary.get("risk_flags_top"))
        risk_line = "; ".join(str(item) for item in risks[:3]) if risks else "-"
        lines = [
            "QuantDesk live summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"Trade date: {summary.get('trade_date') or '-'}",
            f"Snapshot time: {summary.get('snapshot_time') or '-'}",
            f"Market session: {summary.get('market_session') or '-'}",
            f"Watchlist: {summary.get('watchlist_count', 0)}, quotes: {summary.get('quote_count', 0)}",
            f"Partial daily rows: {summary.get('partial_daily_row_count', 0)}",
            f"Intraday scores: {summary.get('intraday_score_count', 0)}",
            f"Condition triggers: {summary.get('triggered_condition_count', 0)}",
            f"Sector heat items: {summary.get('sector_heat_count', 0)}",
            f"Risks: {risk_line}",
            f"Warnings: {summary.get('warning_count', 0)}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if paths:
            lines.append(f"Full JSON: {paths.get('latest_json') or paths.get('json') or next(iter(paths.values()))}")
        if paths.get("latest_report") or paths.get("report"):
            lines.append(f"Report: {paths.get('latest_report') or paths.get('report')}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") == "intraday_live_account_init":
        lines = [
            "QuantDesk live init summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"Path: {summary.get('path') or '-'}",
            f"Created: {str(bool(summary.get('created'))).lower()}",
            f"Overwritten: {str(bool(summary.get('overwritten'))).lower()}",
            f"Initialized: {str(bool(summary.get('live_account_initialized'))).lower()}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if paths:
            lines.append(f"Account state: {paths.get('account_state') or next(iter(paths.values()))}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") == "risk_attribution":
        lines = [
            "QuantDesk attribution summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"Mode: {summary.get('attribution_mode') or '-'}",
            f"Excess return: {summary.get('excess_return')}",
            (
                "Brinson: "
                f"allocation={summary.get('allocation_effect')}, "
                f"selection={summary.get('selection_effect')}, "
                f"interaction={summary.get('interaction_effect')}"
            ),
            f"Factors: {summary.get('factor_count', 0)}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if paths:
            lines.append(f"Full JSON: {paths.get('latest_json') or paths.get('json') or next(iter(paths.values()))}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") == "strategy_recommendation":
        recs = _as_list(summary.get("top_recommendations"))
        top_line = "-"
        if recs and isinstance(recs[0], Mapping):
            top_line = f"{recs[0].get('symbol')} {recs[0].get('primary_strategy')} confidence={recs[0].get('confidence')}"
        lines = [
            "QuantDesk strategy-recommend summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"Mode: {summary.get('mode') or '-'}",
            f"Recommendations: {summary.get('recommendation_count', 0)}",
            f"Top: {top_line}",
            f"Warnings: {summary.get('warning_count', 0)}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if paths:
            lines.append(f"Full JSON: {paths.get('latest_json') or paths.get('json') or next(iter(paths.values()))}")
        if paths.get("latest_report") or paths.get("report"):
            lines.append(f"Report: {paths.get('latest_report') or paths.get('report')}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") in {"risk_attribution_lite", "risk_observation_lite"}:
        risks = _as_list(summary.get("risk_flags_top"))
        risk_line = "; ".join(str(item) for item in risks[:3]) if risks else "-"
        lines = [
            "QuantDesk risk-observation summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"Mode: {summary.get('mode') or 'observation_lite'}",
            f"Compatibility alias: {str(bool(summary.get('compatibility_alias'))).lower()}",
            f"Portfolio: {summary.get('portfolio_name') or '-'}",
            f"Sub-accounts: {summary.get('sub_account_count', 0)}",
            f"Usable factors: {summary.get('usable_factor_count', 0)}, weak factors: {summary.get('weak_factor_count', 0)}",
            f"PIT universe: {str(bool(summary.get('pit_universe'))).lower()}",
            f"Intraday context: {str(bool(summary.get('intraday_context_available'))).lower()}",
            f"Risks: {risk_line}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if paths:
            lines.append(f"Full JSON: {paths.get('latest_json') or paths.get('json') or next(iter(paths.values()))}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    if summary.get("task_type") == "experiments":
        rows = _as_list(summary.get("latest_experiments"))
        top_line = "-"
        if rows and isinstance(rows[0], Mapping):
            top_line = f"{rows[0].get('experiment_id')} ({rows[0].get('task_type')})"
        lines = [
            "QuantDesk experiments summary",
            f"Status: {summary.get('status') or 'unknown'}",
            f"Experiments: {summary.get('experiment_count', 0)}",
            f"Latest: {top_line}",
            f"Diff available: {str(bool(_as_dict(summary.get('diff')).get('available'))).lower()}",
            f"Warnings: {summary.get('warning_count', 0)}",
        ]
        for item in warnings_top[:3]:
            lines.append(f"Warning: {item}")
        if paths:
            lines.append(f"Index: {paths.get('index_csv') or next(iter(paths.values()))}")
        if summary.get("disclaimer"):
            lines.append(str(summary.get("disclaimer")))
        return "\n".join(lines)

    lines = [
        f"QuantDesk {summary.get('task_type') or 'command'} summary",
        f"Status: {summary.get('status') or 'unknown'}",
        f"Target date: {summary.get('target_date') or '-'}",
        f"Network: {str(summary.get('network')).lower()}",
        f"Warnings: {summary.get('warning_count', 0)}",
    ]
    if summary.get("research_dashboard_status"):
        lines.append(
            f"Research dashboard: {summary.get('research_dashboard_status')} "
            f"(readiness={summary.get('research_readiness_level') or '-'}, pit_universe={str(bool(summary.get('pit_universe'))).lower()})"
        )
    if summary.get("multi_strategy_book_status"):
        lines.append(
            f"Multi-strategy book: {summary.get('multi_strategy_book_status')} "
            f"(portfolio={summary.get('multi_strategy_book_portfolio') or '-'}, sub_accounts={summary.get('multi_strategy_book_sub_account_count') or 0})"
        )
    if summary.get("intraday_live_status"):
        lines.append(
            f"Intraday live: {summary.get('intraday_live_status')} "
            f"(snapshot={summary.get('intraday_live_snapshot_time') or '-'}, warnings={summary.get('intraday_live_warning_count') or 0})"
        )
    if summary.get("risk_attribution_status"):
        lines.append(
            f"Risk attribution: {summary.get('risk_attribution_status')} "
            f"(portfolio={summary.get('risk_attribution_portfolio') or '-'}, warnings={summary.get('risk_attribution_warning_count') or 0})"
        )
    if summary.get("experiment_tracker_status"):
        lines.append(
            f"Experiments: {summary.get('experiment_tracker_status')} "
            f"(count={summary.get('experiment_tracker_count') or 0})"
        )
    if summary.get("extended_data_current_gate"):
        lines.append(
            f"Extended data gate: {summary.get('extended_data_current_gate')}"
            f" (preapply={summary.get('extended_import_preapply_gate') or '-'}, "
            f"post={summary.get('extended_import_gate') or '-'})"
        )
        lines.append(
            f"Extended data queue: {summary.get('extended_data_queue_liveness') or 'unknown'}, "
            f"ok={summary.get('extended_data_queue_ok_count', 0)}, "
            f"fail={summary.get('extended_data_queue_fail_count', 0)}, "
            f"traceback={summary.get('extended_data_queue_traceback_count', 0)}, "
            f"stdout_age={summary.get('extended_data_queue_stdout_age_seconds')}s, "
            f"stderr_age={summary.get('extended_data_queue_stderr_age_seconds')}s"
        )
        if summary.get("extended_data_queue_progress_state"):
            lines.append(
                f"Extended data progress: {summary.get('extended_data_queue_progress_state')}"
                f" (ok_delta={summary.get('extended_data_queue_ok_delta')}, "
                f"fail_delta={summary.get('extended_data_queue_fail_delta')}, "
                f"traceback_delta={summary.get('extended_data_queue_traceback_delta')})"
            )
        if summary.get("extended_data_queue_stage_progress"):
            remaining_keys = summary.get("extended_data_queue_stage_remaining_keys")
            remaining_text = ", ".join(str(item) for item in remaining_keys) if isinstance(remaining_keys, list) else "-"
            lines.append(
                f"Extended data stage: {summary.get('extended_data_queue_stage_progress')}"
                f" (completed={summary.get('extended_data_queue_stage_completed_count')}, "
                f"remaining={summary.get('extended_data_queue_stage_remaining_count')}, "
                f"next={remaining_text or '-'}, "
                f"stage_elapsed={summary.get('extended_data_queue_stage_current_elapsed_seconds')}s, "
                f"stage_event_age={summary.get('extended_data_queue_stage_last_event_age_seconds')}s)"
            )
        if summary.get("tushare_minute_target_symbol_count"):
            lines.append(
                f"Tushare minute pool: downloaded={summary.get('tushare_minute_symbol_done_count', 0)}/"
                f"{summary.get('tushare_minute_symbol_count', 0)}, "
                f"available={summary.get('tushare_minute_current_available_count', 0)}/"
                f"{summary.get('tushare_minute_target_symbol_count', 0)}, "
                f"canonical={summary.get('tushare_minute_canonical_current_available_count', 0)}, "
                f"staged_pending_import={summary.get('tushare_minute_staged_current_not_imported_count', 0)}, "
                f"missing_or_stale={summary.get('tushare_minute_missing_or_stale_count', 0)}, "
                f"last={summary.get('tushare_minute_last_symbol') or '-'}, "
                f"status={summary.get('tushare_minute_status') or '-'}, "
                f"traceback={summary.get('tushare_minute_traceback_count', 0)}, "
                f"action={summary.get('tushare_minute_suggested_next_action') or '-'}"
            )
            if summary.get("tushare_minute_status") == "running_with_warnings":
                lines.append(
                    "Tushare minute warning policy: continue monitoring; "
                    "no automatic restart without user confirmation"
                )
        if summary.get("tushare_minute_latest_completed_job_symbol_count"):
            lines.append(
                "Tushare completed minute job: "
                f"available={summary.get('tushare_minute_latest_completed_job_canonical_current_count', 0)}/"
                f"{summary.get('tushare_minute_latest_completed_job_symbol_count', 0)}, "
                f"missing_or_stale={summary.get('tushare_minute_latest_completed_job_missing_or_stale_count', 0)}, "
                f"canonical_files={summary.get('tushare_minute_canonical_file_count', 0)}"
            )
        if summary.get("tushare_minute_latest_completed_quality_total_symbols"):
            lines.append(
                "Tushare completed minute quality: "
                f"status={summary.get('tushare_minute_latest_completed_quality_status') or '-'}, "
                f"usable={summary.get('tushare_minute_latest_completed_quality_usable_count', 0)}/"
                f"{summary.get('tushare_minute_latest_completed_quality_total_symbols', 0)}, "
                f"invalid={summary.get('tushare_minute_latest_completed_quality_invalid_count', 0)}, "
                f"missing={summary.get('tushare_minute_latest_completed_quality_missing_count', 0)}, "
                f"target={summary.get('tushare_minute_latest_completed_quality_target_date') or '-'}"
            )
        if summary.get("extended_data_operator_wait_reason"):
            lines.append(f"Extended data reason: {summary.get('extended_data_operator_wait_reason')}")
        if summary.get("extended_data_manual_review_required"):
            lines.append(f"Extended data manual review: {summary.get('extended_data_manual_review_reason') or '-'}")
        if summary.get("extended_data_requires_user_confirmation"):
            lines.append(
                f"Extended data confirmation required: {summary.get('extended_data_confirmation_required_command') or '-'}"
            )
        domain_counts = summary.get("extended_import_domain_counts")
        if isinstance(domain_counts, dict) and domain_counts:
            domain_text = ", ".join(f"{key}={domain_counts[key]}" for key in sorted(domain_counts))
            lines.append(f"Extended staged domains: {domain_text}")
        if summary.get("extended_import_source_scan_limit_applied"):
            lines.append(
                f"Extended staged counts bounded: max_files_per_rule={summary.get('extended_import_max_files_per_rule')}, "
                f"limited_domains={', '.join(str(item) for item in (summary.get('extended_import_source_scan_limited_domains') or [])) or '-'}"
            )
            full_audit_command = (
                str(summary.get("extended_import_source_scan_full_audit_command") or "").strip()
                or str(summary.get("extended_import_full_audit_command") or "").strip()
            )
            if full_audit_command:
                lines.append(f"Extended staged full audit: {full_audit_command}")
        missing_domains = summary.get("extended_import_missing_planned_domains")
        if isinstance(missing_domains, list) and missing_domains:
            lines.append(f"Extended staged missing domains: {', '.join(str(item) for item in missing_domains)}")
        safe_commands = summary.get("extended_data_safe_next_commands")
        if isinstance(safe_commands, list) and safe_commands:
            lines.append(f"Extended data safe next: {safe_commands[0]}")
    for item in warnings_top[:3]:
        lines.append(f"Warning: {item}")
    if summary.get("suggested_next_action"):
        lines.append(f"Next: {summary.get('suggested_next_action')}")
    if paths:
        first_path = paths.get("json") or paths.get("dashboard_json") or next(iter(paths.values()))
        lines.append(f"Full JSON: {first_path}")
    if summary.get("disclaimer"):
        lines.append(str(summary.get("disclaimer")))
    return "\n".join(lines)
