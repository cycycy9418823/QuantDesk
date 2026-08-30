from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
FULL_AUDIT_COMMAND = "python scripts/qd.py audit-extended-import --summary true --max-files-per-rule 0"
FULL_APPLY_COMMAND = "python scripts/qd.py import-extended-data --apply true --summary true --max-files-per-rule 0"


def test_extended_data_readiness_marks_canonical_intraday_and_staged_domains(tmp_path: Path) -> None:
    from quantdesk.extended_data_readiness import build_extended_data_readiness

    intraday = tmp_path / "data" / "intraday" / "1m"
    intraday.mkdir(parents=True)
    (intraday / "000001.csv").write_text("trade_time,open,close\n2026-07-03 09:31:00,10,10.1\n", encoding="utf-8")

    staging = tmp_path / "data" / "vendor_staging" / "tushare_proxy_extended_full_20260705_010203"
    (staging / "raw" / "ths").mkdir(parents=True)
    (staging / "raw" / "ths" / "ths_index.csv").write_text("ts_code,name\n885001.TI,test\n", encoding="utf-8")
    (staging / "raw" / "by_symbol" / "income").mkdir(parents=True)
    (staging / "raw" / "by_symbol" / "income" / "000001.SZ.csv").write_text("ts_code,end_date\n000001.SZ,20251231\n", encoding="utf-8")
    (staging / "raw" / "by_trade_date" / "moneyflow").mkdir(parents=True)
    (staging / "raw" / "by_trade_date" / "moneyflow" / "20260703.csv").write_text("ts_code,buy_sm_amount\n000001.SZ,1\n", encoding="utf-8")

    queue = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_020304"
    queue.mkdir(parents=True)
    (queue / "queue_status.json").write_text(
        json.dumps({"status": "running", "current_stage": "minute_retry_partial"}, ensure_ascii=False),
        encoding="utf-8",
    )

    payload = build_extended_data_readiness(project_root=tmp_path)

    assert payload["task_type"] == "extended_data_readiness"
    assert payload["domains"]["intraday_1m"]["status"] == "partial"
    assert payload["domains"]["intraday_1m"]["file_count"] == 1
    assert payload["domains"]["intraday_1m"]["latest_date"] == "2026-07-03"
    assert payload["domains"]["sector_index"]["status"] == "staged_not_imported"
    assert payload["domains"]["fundamentals"]["status"] == "staged_not_imported"
    assert payload["domains"]["moneyflow"]["status"] == "staged_not_imported"
    assert payload["download_queue"]["status"] == "running"
    assert payload["download_queue"]["current_stage"] == "minute_retry_partial"
    assert payload["capabilities"]["mainline_analysis"] in {"partial", "degraded"}
    assert any("staged_not_imported" in warning for warning in payload["warnings"])
    assert "download_queue_still_running" in payload["warnings"]
    assert payload["suggested_next_action"] == "wait_for_queue_then_rerun_audit"


def test_extended_data_readiness_suggests_full_audit_for_staged_data_after_queue_completion(tmp_path: Path) -> None:
    from quantdesk.extended_data_readiness import build_extended_data_readiness

    staging = tmp_path / "data" / "vendor_staging" / "tushare_proxy_extended_full_20260705_010203"
    (staging / "raw" / "by_trade_date" / "moneyflow").mkdir(parents=True)
    (staging / "raw" / "by_trade_date" / "moneyflow" / "20260703.csv").write_text(
        "ts_code,buy_sm_amount\n000001.SZ,1\n",
        encoding="utf-8",
    )
    queue = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_020304"
    queue.mkdir(parents=True)
    (queue / "queue_status.json").write_text(
        json.dumps({"status": "completed", "current_stage": "harvest_moneyflow"}, ensure_ascii=False),
        encoding="utf-8",
    )

    payload = build_extended_data_readiness(project_root=tmp_path)

    assert payload["download_queue"]["status"] == "completed"
    assert payload["domains"]["moneyflow"]["status"] == "staged_not_imported"
    assert payload["suggested_next_action"] == FULL_AUDIT_COMMAND


def test_extended_data_readiness_missing_domains_are_explicit(tmp_path: Path) -> None:
    from quantdesk.extended_data_readiness import build_extended_data_readiness

    payload = build_extended_data_readiness(project_root=tmp_path)

    assert payload["status"] == "missing"
    assert payload["domains"]["sector_index"]["status"] == "missing"
    assert payload["domains"]["events"]["status"] == "missing"
    assert payload["capabilities"]["mainline_analysis"] == "degraded"
    assert "missing:sector_index" in payload["warnings"]


def test_metadata_core_surfaces_missing_st_and_delist_components(tmp_path: Path) -> None:
    from quantdesk.extended_data_readiness import build_extended_data_readiness

    metadata = tmp_path / "data" / "metadata"
    metadata.mkdir(parents=True)
    for name in ("adjustment_factors.csv", "suspend_d.csv", "stk_limit.csv"):
        (metadata / name).write_text("ts_code,trade_date\n000001.SZ,20260703\n", encoding="utf-8")

    payload = build_extended_data_readiness(project_root=tmp_path)
    metadata_core = payload["domains"]["metadata_core"]

    assert metadata_core["status"] == "available"
    assert metadata_core["component_status"]["st_status_history"]["status"] == "missing"
    assert metadata_core["component_status"]["delist_history"]["status"] == "missing"
    assert "missing_metadata_component:st_status_history" in metadata_core["warnings"]
    assert "missing_metadata_component:delist_history" in payload["warnings"]


def test_metadata_core_accepts_project_schema_filenames_as_canonical(tmp_path: Path) -> None:
    from quantdesk.extended_data_readiness import build_extended_data_readiness

    metadata = tmp_path / "data" / "metadata"
    metadata.mkdir(parents=True)
    for name in (
        "adjustment_factors.csv",
        "delist_history.csv",
        "st_history.csv",
        "suspension_history.csv",
        "limit_price_history.csv",
    ):
        (metadata / name).write_text("symbol,trade_date\n000001,20260703\n", encoding="utf-8")

    payload = build_extended_data_readiness(project_root=tmp_path)
    metadata_core = payload["domains"]["metadata_core"]

    assert metadata_core["status"] == "available"
    assert metadata_core["component_status"]["st_status_history"]["status"] == "available"
    assert metadata_core["component_status"]["suspend_d"]["status"] == "available"
    assert metadata_core["component_status"]["stk_limit"]["status"] == "available"
    assert not any(
        warning.startswith("missing_metadata_component:") or warning.startswith("staged_metadata_component_not_imported:")
        for warning in metadata_core["warnings"]
    )


def test_extended_data_readiness_derives_queue_stage_from_stage_history(tmp_path: Path) -> None:
    from quantdesk.extended_data_readiness import build_extended_data_readiness

    queue = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310"
    queue.mkdir(parents=True)
    (queue / "queue_status.json").write_text(
        json.dumps(
            {
                "status": "running",
                "stages": [
                    {"key": "minute_retry_partial", "status": "completed", "updated_at": "2026-07-05T03:05:31"},
                    {"key": "harvest_index_sectors", "status": "starting", "updated_at": "2026-07-05T03:07:04"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = build_extended_data_readiness(project_root=tmp_path)

    assert payload["download_queue"]["status"] == "running"
    assert payload["download_queue"]["current_stage"] == "harvest_index_sectors"
    assert payload["download_queue"]["current_stage_status"] == "starting"
    assert payload["download_queue"]["updated_at"] == "2026-07-05T03:07:04"


def test_extended_data_readiness_detects_staged_1min_alias(tmp_path: Path) -> None:
    from quantdesk.extended_data_readiness import build_extended_data_readiness

    minute_dir = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310" / "minute_partial_retry" / "raw" / "1min"
    minute_dir.mkdir(parents=True)
    (minute_dir / "000001.csv").write_text("trade_time,open,close\n2026-07-03 09:31:00,10,10.1\n", encoding="utf-8")

    payload = build_extended_data_readiness(project_root=tmp_path)

    assert payload["domains"]["intraday_1m"]["status"] == "staged_not_imported"
    assert payload["domains"]["intraday_1m"]["file_count"] == 1
    assert payload["domains"]["intraday_1m"]["latest_date"] == "2026-07-03"


def test_write_extended_data_readiness_outputs_writes_json_and_markdown(tmp_path: Path) -> None:
    from quantdesk.extended_data_readiness import write_extended_data_readiness_outputs

    payload = write_extended_data_readiness_outputs(
        project_root=tmp_path,
        output_path=tmp_path / "results" / "data_health" / "extended_data_readiness_latest.json",
        report_path=tmp_path / "reports" / "extended_data_readiness_latest.md",
    )

    assert payload["task_type"] == "extended_data_readiness"
    assert (tmp_path / "results" / "data_health" / "extended_data_readiness_latest.json").exists()
    report = (tmp_path / "reports" / "extended_data_readiness_latest.md").read_text(encoding="utf-8")
    assert "Extended Data Readiness" in report
    assert "staged_not_imported" in report
    assert "Not Investment Advice" in report


def test_data_readiness_cli_and_qd_help_contract(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            PYTHON,
            str(PROJECT_ROOT / "scripts" / "show_extended_data_readiness.py"),
            "--project-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "readiness.json"),
            "--report",
            str(tmp_path / "readiness.md"),
            "--json",
            "true",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["task_type"] == "extended_data_readiness"
    assert (tmp_path / "readiness.json").exists()
    assert (tmp_path / "readiness.md").exists()

    help_text = subprocess.check_output(
        [PYTHON, str(PROJECT_ROOT / "scripts" / "qd.py"), "help"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert "data-readiness" in help_text


def test_data_readiness_is_discoverable_from_command_center_and_artifacts(tmp_path: Path) -> None:
    from quantdesk.command_center_status import build_command_center_status
    from quantdesk.operation_log import build_operation_record
    from quantdesk.workbench_overview import build_workbench_artifacts

    log_path = tmp_path / "operation_log.jsonl"
    log_path.write_text(
        json.dumps(build_operation_record(command="data-readiness", status="warning", output_paths=["results/data_health/extended_data_readiness_latest.json"]), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    command_center = build_command_center_status(log_path=log_path)
    command_names = [row.get("command") for row in command_center["commands"]]
    assert "data-readiness" in command_names
    assert "tushare-queue-status" in command_names
    assert "tushare-retry-plan" in command_names
    assert "audit-extended-import" in command_names
    assert "verify-extended-import" in command_names

    artifacts = build_workbench_artifacts(project_root=tmp_path)
    text = json.dumps(artifacts, ensure_ascii=False)
    assert "extended_data_readiness_latest.json" in text
    assert "extended_data_readiness_latest.md" in text
    assert "tushare_queue_status_latest.json" in text
    assert "tushare_queue_status_latest.md" in text
    assert "tushare_retry_plan_latest.json" in text
    assert "tushare_retry_plan_latest.md" in text
    assert "extended_staged_import_audit_latest.json" in text
    assert "extended_staged_import_audit_latest.md" in text
    assert "extended_import_verification_latest.json" in text
    assert "extended_import_verification_latest.md" in text
    flat = [item for items in artifacts["groups"].values() for item in items]
    command_by_module = {item["module"]: item["remediation_command"] for item in flat}
    assert command_by_module["data-readiness"] == "python scripts/qd.py data-readiness --summary true"
    assert command_by_module["data-gates"] == "python scripts/qd.py data-gates --summary true"
    assert command_by_module["tushare-queue-status"] == "python scripts/qd.py tushare-queue-status --summary true"
    assert command_by_module["tushare-retry-plan"] == "python scripts/qd.py tushare-retry-plan --summary true"
    assert command_by_module["audit-extended-import"] == FULL_AUDIT_COMMAND
    assert command_by_module["verify-extended-import"] == "python scripts/qd.py verify-extended-import --summary true"
    assert command_by_module["command-center"] == "python scripts/qd.py command-center --summary true"


def test_workbench_extended_data_specs_prefer_summary_commands() -> None:
    from quantdesk.workbench_overview import _spec_command

    assert _spec_command("data-readiness") == "python scripts/qd.py data-readiness --summary true"
    assert _spec_command("data-gates") == "python scripts/qd.py data-gates --summary true"
    assert _spec_command("tushare-queue-status") == "python scripts/qd.py tushare-queue-status --summary true"
    assert _spec_command("tushare-retry-plan") == "python scripts/qd.py tushare-retry-plan --summary true"
    assert _spec_command("audit-extended-import") == FULL_AUDIT_COMMAND
    assert _spec_command("verify-extended-import") == "python scripts/qd.py verify-extended-import --summary true"
    assert _spec_command("command-center") == "python scripts/qd.py command-center --summary true"


def test_command_center_data_health_row_exposes_extended_data_gates(tmp_path: Path, monkeypatch) -> None:
    from quantdesk import command_center_status as ccs
    from quantdesk.operation_log import build_operation_record

    result_path = tmp_path / "results" / "data_health" / "data_health_dashboard_latest.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(
            {
                "status": "warning",
                "target_date": "2026-07-03",
                "warnings": ["download_queue_still_running"],
                "suggested_next_action": "wait_for_queue_then_audit_and_import",
                "output_paths": {
                    "dashboard_json": "results/data_health/data_health_dashboard_latest.json",
                    "report": "reports/data_health_dashboard_report_latest.md",
                },
                "data_health_summary": {
                    "tushare_queue_status": "running",
                    "tushare_queue_stage": "harvest_fundamentals",
                    "extended_data_current_gate": "download_queue",
                    "extended_data_gate_suggested_next_action": "continue_monitoring",
                    "extended_data_queue_ok_count": 12,
                    "extended_data_queue_fail_count": 1,
                    "extended_data_queue_traceback_count": 0,
                    "extended_data_queue_stdout_age_seconds": 5,
                        "extended_data_queue_stderr_age_seconds": 7,
                        "extended_data_queue_liveness": "active",
                        "extended_data_queue_progress_state": "advancing",
                        "extended_data_queue_ok_delta": 3,
                        "extended_data_queue_fail_delta": 0,
                        "extended_data_queue_traceback_delta": 0,
                        "tushare_minute_canonical_current_available_count": 12,
                        "tushare_minute_staged_current_not_imported_count": 5,
                        "extended_data_operator_wait_reason": "download_queue_active",
                        "extended_data_manual_review_required": False,
                        "extended_data_manual_review_reason": "",
                        "extended_data_requires_user_confirmation": False,
                        "extended_data_confirmation_required_command": "",
                        "extended_import_domain_counts": {"events": 5, "fundamentals": 7},
                        "extended_import_missing_planned_domains": ["moneyflow"],
                        "extended_import_duplicate_destination_count": 2,
                        "extended_import_existing_destination_count": 1,
                        "extended_import_would_improve_domains": ["events", "fundamentals"],
                        "extended_import_max_files_per_rule": 20,
                        "extended_import_source_scan_limit_applied": True,
                        "extended_import_source_scan_limited_domains": ["events"],
                        "extended_import_full_audit_command": FULL_AUDIT_COMMAND,
                        "extended_import_source_scan_full_audit_command": FULL_AUDIT_COMMAND,
                        "extended_data_safe_next_commands": [
                            "python scripts/qd.py tushare-queue-status --summary true",
                            "python scripts/qd.py data-gates --summary true",
                        ],
                        "extended_data_blocked_without_confirmation": [
                            "python scripts/qd.py import-extended-data --apply true",
                        ],
                        "tushare_minute_status": "running",
                        "tushare_minute_coverage_status": "download_in_progress",
                        "tushare_minute_symbol_done_count": 7,
                        "tushare_minute_symbol_count": 2656,
                        "tushare_minute_last_symbol": "920010",
                        "tushare_minute_target_symbol_count": 2656,
                        "tushare_minute_current_available_count": 7,
                        "tushare_minute_missing_or_stale_count": 2649,
                        "tushare_minute_current_available_ratio": 0.002635,
                        "tushare_minute_coverage_suggested_next_action": "continue_monitoring_minute_download",
                        "tushare_retry_gate": "wait_for_queue_completion",
                        "extended_import_preapply_gate": "wait_for_download_completion",
                        "extended_import_gate": "wait_for_download_completion",
                    "extended_import_canonical_presence_ratio": 0.571429,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "operation_log.jsonl"
    log_path.write_text(
        json.dumps(
            build_operation_record(
                command="data-health",
                status="warning",
                output_paths=["results/data_health/data_health_dashboard_latest.json"],
            ),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ccs, "PROJECT_ROOT", tmp_path)

    payload = ccs.build_command_center_status(log_path=log_path)
    row = next(item for item in payload["commands"] if item["command"] == "data-health")

    assert row["last_status"] == "warning"
    assert row["suggested_next_action"] == "wait_for_queue_then_audit_and_import"
    assert payload["suggested_next_action"] == "python scripts/qd.py tushare-queue-status --summary true"
    assert row["data_gate_summary"]["extended_data_current_gate"] == "download_queue"
    assert row["data_gate_summary"]["extended_data_gate_suggested_next_action"] == "continue_monitoring"
    assert row["data_gate_summary"]["extended_data_queue_ok_count"] == 12
    assert row["data_gate_summary"]["extended_data_queue_fail_count"] == 1
    assert row["data_gate_summary"]["extended_data_queue_traceback_count"] == 0
    assert row["data_gate_summary"]["extended_data_queue_stdout_age_seconds"] == 5
    assert row["data_gate_summary"]["extended_data_queue_stderr_age_seconds"] == 7
    assert row["data_gate_summary"]["extended_data_queue_liveness"] == "active"
    assert row["data_gate_summary"]["extended_data_queue_progress_state"] == "advancing"
    assert row["data_gate_summary"]["extended_data_queue_ok_delta"] == 3
    assert row["data_gate_summary"]["extended_data_queue_fail_delta"] == 0
    assert row["data_gate_summary"]["extended_data_queue_traceback_delta"] == 0
    assert row["data_gate_summary"]["extended_data_operator_wait_reason"] == "download_queue_active"
    assert row["data_gate_summary"]["extended_data_manual_review_required"] is False
    assert row["data_gate_summary"]["extended_data_manual_review_reason"] == ""
    assert row["data_gate_summary"]["extended_data_requires_user_confirmation"] is False
    assert row["data_gate_summary"]["extended_data_confirmation_required_command"] == ""
    assert row["data_gate_summary"]["tushare_minute_status"] == "running"
    assert row["data_gate_summary"]["tushare_minute_symbol_done_count"] == 7
    assert row["data_gate_summary"]["tushare_minute_symbol_count"] == 2656
    assert row["data_gate_summary"]["tushare_minute_last_symbol"] == "920010"
    assert row["data_gate_summary"]["tushare_minute_current_available_count"] == 7
    assert row["data_gate_summary"]["tushare_minute_missing_or_stale_count"] == 2649
    assert row["data_gate_summary"]["extended_import_domain_counts"] == {"events": 5, "fundamentals": 7}
    assert row["data_gate_summary"]["extended_import_missing_planned_domains"] == ["moneyflow"]
    assert row["data_gate_summary"]["extended_import_duplicate_destination_count"] == 2
    assert row["data_gate_summary"]["extended_import_existing_destination_count"] == 1
    assert row["data_gate_summary"]["extended_import_would_improve_domains"] == ["events", "fundamentals"]
    assert row["data_gate_summary"]["extended_import_max_files_per_rule"] == 20
    assert row["data_gate_summary"]["extended_import_source_scan_limit_applied"] is True
    assert row["data_gate_summary"]["extended_import_source_scan_limited_domains"] == ["events"]
    assert row["data_gate_summary"]["extended_import_full_audit_command"] == FULL_AUDIT_COMMAND
    assert row["data_gate_summary"]["extended_import_source_scan_full_audit_command"] == FULL_AUDIT_COMMAND
    assert "python scripts/qd.py tushare-queue-status --summary true" in row["data_gate_summary"]["extended_data_safe_next_commands"]
    assert "python scripts/qd.py import-extended-data --apply true" in row["data_gate_summary"]["extended_data_blocked_without_confirmation"]
    assert row["data_gate_summary"]["tushare_queue_stage"] == "harvest_fundamentals"
    assert row["data_gate_summary"]["tushare_retry_gate"] == "wait_for_queue_completion"
    assert row["data_gate_summary"]["extended_import_preapply_gate"] == "wait_for_download_completion"
    assert row["data_gate_summary"]["extended_import_gate"] == "wait_for_download_completion"


def test_command_center_prefers_latest_data_gates_snapshot_over_stale_data_health(tmp_path: Path, monkeypatch) -> None:
    from quantdesk import command_center_status as ccs
    from quantdesk.operation_log import build_operation_record

    data_health_dir = tmp_path / "results" / "data_health"
    data_health_dir.mkdir(parents=True)
    (data_health_dir / "data_health_dashboard_latest.json").write_text(
        json.dumps(
            {
                "status": "warning",
                "suggested_next_action": "wait_for_queue_then_audit_and_import",
                "output_paths": {
                    "dashboard_json": "results/data_health/data_health_dashboard_latest.json",
                    "report": "reports/data_health_dashboard_report_latest.md",
                },
                "data_health_summary": {
                    "extended_data_current_gate": "download_queue",
                    "extended_data_gate_suggested_next_action": "wait_for_queue_then_audit_and_import",
                    "extended_data_queue_ok_count": 12,
                    "extended_data_queue_fail_count": 1,
                    "extended_data_queue_traceback_count": 0,
                    "extended_data_queue_liveness": "active",
                    "extended_import_preapply_gate": "wait_for_download_completion",
                    "extended_import_gate": "wait_for_download_completion",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_health_dir / "extended_data_gates_latest.json").write_text(
        json.dumps(
            {
                "task_type": "extended_data_gates",
                "status": "warning",
                "current_gate": "download_queue",
                "suggested_next_action": "continue_monitoring",
                "operator_wait_reason": "download_queue_active",
                "operator_action_policy": {
                    "safe_next_commands": ["python scripts/qd.py tushare-queue-status --summary true"],
                    "blocked_without_confirmation": ["python scripts/qd.py import-extended-data --apply true"],
                    "manual_review_required": False,
                    "manual_review_reason": "",
                    "requires_user_confirmation": False,
                    "confirmation_required_command": "",
                },
                "gate_chain": {
                    "queue": {
                        "status": "running",
                        "current_stage": "harvest_fundamentals",
                        "ok_count": 99,
                        "fail_count": 2,
                        "ssl_eof_count": 3,
                        "timeout_count": 4,
                        "traceback_count": 0,
                        "stdout_age_seconds": 6,
                        "stderr_age_seconds": 9,
                        "queue_liveness": "active",
                        "progress_state": "advancing",
                        "stage_position_label": "6/7",
                        "stage_requested_count": 7,
                        "stage_completed_count": 4,
                        "stage_remaining_count": 1,
                        "stage_remaining_keys": ["harvest_moneyflow"],
                        "ok_delta": 7,
                        "fail_delta": 0,
                        "traceback_delta": 0,
                    },
                    "minute": {
                        "status": "running",
                        "coverage_status": "download_in_progress",
                        "symbol_done_count": 17,
                        "symbol_count": 2656,
                        "last_symbol": "920030",
                        "target_symbol_count": 2656,
                        "current_available_count": 17,
                        "missing_or_stale_count": 2639,
                        "current_available_ratio": 0.006401,
                        "latest_completed_quality_status": "passed",
                        "latest_completed_quality_total_symbols": 200,
                        "latest_completed_quality_usable_count": 200,
                        "latest_completed_quality_invalid_count": 0,
                        "latest_completed_quality_missing_count": 0,
                        "latest_completed_quality_target_date": "2026-07-03",
                        "suggested_next_action": "continue_monitoring_minute_download",
                    },
                    "retry_plan": {"retry_gate": "wait_for_queue_completion"},
                    "import_audit": {
                        "preapply_gate": "wait_for_download_completion",
                        "domain_counts": {"events": 5},
                        "missing_planned_domains": [],
                        "duplicate_destination_count": 0,
                        "existing_destination_count": 0,
                        "would_improve_domains": ["events"],
                        "max_files_per_rule": 20,
                        "source_scan_limit_applied": True,
                        "source_scan_limited_domains": ["events"],
                        "source_scan_limited_rule_count": 1,
                        "full_audit_command": FULL_AUDIT_COMMAND,
                        "source_scan_full_audit_command": FULL_AUDIT_COMMAND,
                    },
                    "import_verification": {
                        "post_import_gate": "wait_for_download_completion",
                        "canonical_presence_ratio": 0.5,
                    },
                },
                "generated_at": "2026-07-05T16:30:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "operation_log.jsonl"
    log_path.write_text(
        json.dumps(
            build_operation_record(
                command="data-health",
                status="warning",
                suggested_next_action="wait_for_queue_then_audit_and_import",
                output_paths=["results/data_health/data_health_dashboard_latest.json"],
            ),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ccs, "PROJECT_ROOT", tmp_path)

    payload = ccs.build_command_center_status(log_path=log_path)
    row = next(item for item in payload["commands"] if item["command"] == "data-health")

    assert row["suggested_next_action"] == "continue_monitoring"
    assert payload["suggested_next_action"] == "python scripts/qd.py tushare-queue-status --summary true"
    assert row["data_gate_summary"]["data_gate_summary_source"] == "extended_data_gates_latest"
    assert row["data_gate_summary"]["data_gate_summary_generated_at"] == "2026-07-05T16:30:00"
    assert row["data_gate_summary"]["extended_data_queue_ok_count"] == 99
    assert row["data_gate_summary"]["extended_data_queue_fail_count"] == 2
    assert row["data_gate_summary"]["extended_data_queue_ssl_eof_count"] == 3
    assert row["data_gate_summary"]["extended_data_queue_timeout_count"] == 4
    assert row["data_gate_summary"]["extended_data_queue_ok_delta"] == 7
    assert row["data_gate_summary"]["extended_data_queue_stage_progress"] == "6/7"
    assert row["data_gate_summary"]["extended_data_queue_stage_remaining_count"] == 1
    assert row["data_gate_summary"]["extended_data_queue_stage_remaining_keys"] == ["harvest_moneyflow"]
    assert row["data_gate_summary"]["tushare_minute_symbol_done_count"] == 17
    assert row["data_gate_summary"]["tushare_minute_last_symbol"] == "920030"
    assert row["data_gate_summary"]["tushare_minute_missing_or_stale_count"] == 2639
    assert row["data_gate_summary"]["tushare_minute_latest_completed_quality_status"] == "passed"
    assert row["data_gate_summary"]["tushare_minute_latest_completed_quality_usable_count"] == 200
    assert row["data_gate_summary"]["tushare_minute_latest_completed_quality_total_symbols"] == 200
    assert row["data_gate_summary"]["extended_import_domain_counts"] == {"events": 5}
    assert row["data_gate_summary"]["extended_import_full_audit_command"] == FULL_AUDIT_COMMAND


def test_command_center_console_lines_surface_data_gate_counts() -> None:
    from scripts.show_command_center_status import _data_gate_console_lines

    lines = _data_gate_console_lines(
        {
            "commands": [
                {
                    "command": "data-health",
                    "data_gate_summary": {
                        "extended_data_current_gate": "download_queue",
                        "data_gate_summary_source": "extended_data_gates_latest",
                        "data_gate_summary_generated_at": "2026-07-05T16:30:00",
                        "extended_data_gate_suggested_next_action": "continue_monitoring",
                        "extended_data_queue_ok_count": 12,
                        "extended_data_queue_fail_count": 1,
                        "extended_data_queue_ssl_eof_count": 2,
                        "extended_data_queue_timeout_count": 3,
                        "extended_data_queue_traceback_count": 0,
                        "extended_data_queue_stdout_age_seconds": 5,
                        "extended_data_queue_stderr_age_seconds": 7,
                        "extended_data_queue_liveness": "active",
                        "extended_data_queue_progress_state": "advancing",
                        "extended_data_queue_stage_progress": "6/7",
                        "extended_data_queue_stage_completed_count": 4,
                        "extended_data_queue_stage_remaining_count": 1,
                        "extended_data_queue_stage_remaining_keys": ["harvest_moneyflow"],
                        "extended_data_queue_ok_delta": 3,
                        "extended_data_queue_fail_delta": 0,
                        "extended_data_queue_traceback_delta": 0,
                        "tushare_minute_symbol_done_count": 17,
                        "tushare_minute_symbol_count": 2656,
                        "tushare_minute_last_symbol": "920030",
                        "tushare_minute_target_symbol_count": 2656,
                        "tushare_minute_current_available_count": 17,
                        "tushare_minute_canonical_current_available_count": 12,
                        "tushare_minute_staged_current_not_imported_count": 5,
                        "tushare_minute_missing_or_stale_count": 2639,
                        "tushare_minute_latest_completed_quality_status": "passed",
                        "tushare_minute_latest_completed_quality_total_symbols": 200,
                        "tushare_minute_latest_completed_quality_usable_count": 200,
                        "tushare_minute_latest_completed_quality_invalid_count": 0,
                        "tushare_minute_latest_completed_quality_missing_count": 0,
                        "tushare_minute_latest_completed_quality_target_date": "2026-07-03",
                        "extended_data_operator_wait_reason": "download_queue_active",
                        "extended_data_manual_review_required": False,
                        "extended_data_manual_review_reason": "",
                        "extended_import_domain_counts": {"events": 5, "fundamentals": 7},
                        "extended_import_missing_planned_domains": ["moneyflow"],
                        "extended_import_max_files_per_rule": 20,
                        "extended_import_source_scan_limit_applied": True,
                        "extended_import_source_scan_limited_domains": ["events"],
                        "extended_import_full_audit_command": FULL_AUDIT_COMMAND,
                        "extended_import_source_scan_full_audit_command": FULL_AUDIT_COMMAND,
                        "extended_data_safe_next_commands": [
                            "python scripts/qd.py tushare-queue-status --summary true",
                        ],
                        "extended_data_blocked_without_confirmation": [
                            "python scripts/qd.py import-extended-data --apply true",
                        ],
                    },
                }
            ]
        }
    )

    assert lines == [
        "data-gates: download_queue / active / ok=12 / fail=1 / ssl_eof=2 / timeout=3 / traceback=0 / stdout_age=5s / stderr_age=7s",
        "data-gates source: extended_data_gates_latest / generated_at=2026-07-05T16:30:00",
        "data-gates progress: advancing / ok_delta=3 / fail_delta=0 / traceback_delta=0",
        "data-gates queue stage: 6/7 / completed=4 / remaining=1 / next=harvest_moneyflow",
        "data-gates minute pool: downloaded=17/2656, available=17/2656, canonical=12, staged_pending_import=5, missing_or_stale=2639, last=920030",
        "data-gates completed minute quality: status=passed, usable=200/200, invalid=0, missing=0, target=2026-07-03",
        "data-gates staged domains: events=5, fundamentals=7",
        "data-gates staged domain counts are bounded: max_files_per_rule=20, limited_domains=events",
        f"data-gates full audit: {FULL_AUDIT_COMMAND}",
        "data-gates missing staged domains: moneyflow",
        "data-gates reason: download_queue_active",
        "data-gates safe next: python scripts/qd.py tushare-queue-status --summary true",
        "data-gates blocked without confirmation: python scripts/qd.py import-extended-data --apply true",
        "data-gates next: continue_monitoring",
    ]


def test_command_center_console_lines_surface_stage_timing_when_measured() -> None:
    from scripts.show_command_center_status import _data_gate_console_lines

    lines = _data_gate_console_lines(
        {
            "commands": [
                {
                    "command": "data-health",
                    "data_gate_summary": {
                        "extended_data_current_gate": "download_queue",
                        "extended_data_queue_stage_progress": "6/7",
                        "extended_data_queue_stage_completed_count": 5,
                        "extended_data_queue_stage_remaining_count": 1,
                        "extended_data_queue_stage_remaining_keys": ["harvest_moneyflow"],
                        "extended_data_queue_stage_current_elapsed_seconds": 123.9,
                        "extended_data_queue_stage_last_event_age_seconds": "45.2",
                    },
                }
            ]
        }
    )

    assert lines[1] == (
        "data-gates queue stage: 6/7 / completed=5 / remaining=1 / "
        "next=harvest_moneyflow / stage_elapsed=123s / stage_event_age=45s"
    )


def test_data_health_dashboard_includes_extended_readiness(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            PYTHON,
            str(PROJECT_ROOT / "scripts" / "run_data_health_dashboard.py"),
            "--symbols",
            "000001",
            "--network",
            "false",
            "--max-symbols",
            "1",
            "--include-freshness",
            "false",
            "--include-intraday",
            "false",
            "--include-provider-stats",
            "false",
            "--include-grade-trend",
            "false",
            "--output-dir",
            str(tmp_path / "data_health"),
            "--json",
            "true",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["extended_data_readiness"]["task_type"] == "extended_data_readiness"
    assert payload["tushare_queue_status"]["task_type"] == "tushare_queue_status"
    assert payload["tushare_minute_status"]["task_type"] == "tushare_minute_status"
    assert payload["tushare_minute_coverage"]["task_type"] == "tushare_minute_coverage"
    assert payload["tushare_retry_plan"]["task_type"] == "tushare_retry_plan"
    assert payload["extended_import_audit"]["task_type"] == "extended_staged_import_audit"
    assert payload["extended_import_verification"]["task_type"] == "extended_import_verification"
    assert payload["extended_data_gates"]["task_type"] == "extended_data_gates"
    assert payload["data_health_summary"]["extended_data_status"] in {"missing", "partial", "passed"}
    assert payload["data_health_summary"]["extended_data_current_gate"] in {
        "download_queue",
        "retry_plan",
        "preapply_audit",
        "post_import_verification",
        "canonical_presence_verified",
        "unknown",
    }
    assert "extended_data_queue_ok_count" in payload["data_health_summary"]
    assert "extended_data_queue_fail_count" in payload["data_health_summary"]
    assert "extended_data_queue_traceback_count" in payload["data_health_summary"]
    assert "extended_data_queue_stdout_age_seconds" in payload["data_health_summary"]
    assert "extended_data_queue_stderr_age_seconds" in payload["data_health_summary"]
    assert "extended_data_queue_liveness" in payload["data_health_summary"]
    assert "extended_data_queue_progress_state" in payload["data_health_summary"]
    assert "extended_data_queue_ok_delta" in payload["data_health_summary"]
    assert "extended_data_queue_fail_delta" in payload["data_health_summary"]
    assert "extended_data_queue_traceback_delta" in payload["data_health_summary"]
    assert "extended_data_operator_wait_reason" in payload["data_health_summary"]
    assert "extended_data_manual_review_required" in payload["data_health_summary"]
    assert "extended_data_manual_review_reason" in payload["data_health_summary"]
    assert "extended_data_requires_user_confirmation" in payload["data_health_summary"]
    assert "extended_data_confirmation_required_command" in payload["data_health_summary"]
    assert "extended_data_safe_next_commands" in payload["data_health_summary"]
    assert "extended_data_blocked_without_confirmation" in payload["data_health_summary"]
    assert "extended_import_domain_counts" in payload["data_health_summary"]
    assert "extended_import_missing_planned_domains" in payload["data_health_summary"]
    assert "extended_import_duplicate_destination_count" in payload["data_health_summary"]
    assert "extended_import_existing_destination_count" in payload["data_health_summary"]
    assert "extended_import_would_improve_domains" in payload["data_health_summary"]
    assert "extended_import_max_files_per_rule" in payload["data_health_summary"]
    assert "extended_import_source_scan_limit_applied" in payload["data_health_summary"]
    assert "extended_import_source_scan_limited_domains" in payload["data_health_summary"]
    assert "extended_import_full_audit_command" in payload["data_health_summary"]
    assert "extended_import_source_scan_full_audit_command" in payload["data_health_summary"]
    assert payload["data_health_summary"]["tushare_queue_status"] in {"missing", "running", "unknown"}
    assert payload["data_health_summary"]["tushare_minute_status"] in {
        "missing",
        "waiting_for_secure_token_input",
        "starting",
        "running",
        "running_with_warnings",
        "recent_activity_no_process_match",
        "stale_or_stopped",
        "completed",
        "stopped_rate_or_permission",
        "error_traceback",
    }
    assert "tushare_minute_symbol_done_count" in payload["data_health_summary"]
    assert "tushare_minute_symbol_count" in payload["data_health_summary"]
    assert "tushare_minute_last_symbol" in payload["data_health_summary"]
    assert "tushare_minute_coverage_status" in payload["data_health_summary"]
    assert "tushare_minute_target_symbol_count" in payload["data_health_summary"]
    assert "tushare_minute_current_available_count" in payload["data_health_summary"]
    assert "tushare_minute_missing_or_stale_count" in payload["data_health_summary"]
    assert "tushare_minute_current_available_ratio" in payload["data_health_summary"]
    assert "tushare_minute_coverage_suggested_next_action" in payload["data_health_summary"]
    assert payload["data_health_summary"]["tushare_retry_gate"] in {
        "no_queue_found",
        "wait_for_queue_completion",
        "no_retry_needed",
        "review_failures_before_retry",
        "inspect_traceback_before_retry",
        "review_retries",
    }
    assert payload["data_health_summary"]["extended_import_gate"] in {
        "wait_for_download_completion",
        "no_canonical_import_yet",
        "partial_canonical_import",
        "canonical_presence_verified",
    }
    assert payload["data_health_summary"]["extended_import_preapply_gate"] in {
        "wait_for_download_completion",
        "wait_for_download_completion_queue_stale_manual_review",
        "no_staged_files_found",
        "review_required",
        "ready_for_bounded_apply",
        "ready_for_full_apply",
    }
    assert "extended_data_readiness" in payload["output_paths"]
    assert "tushare_queue_status" in payload["output_paths"]
    assert "tushare_minute_status" in payload["output_paths"]
    assert "tushare_minute_coverage" in payload["output_paths"]
    assert "tushare_retry_plan" in payload["output_paths"]
    assert "extended_import_audit" in payload["output_paths"]
    assert "extended_import_verification" in payload["output_paths"]
    assert "extended_data_gates" in payload["output_paths"]


def test_data_health_suggested_action_prioritizes_download_and_import_gates() -> None:
    from scripts.run_data_health_dashboard import _choose_suggested_next_action

    suggested = _choose_suggested_next_action(
        freshness={"status": "warning", "suggested_next_action": "refresh_daily_data"},
        intraday={"status": "warning", "suggested_next_action": "refresh_intraday"},
        provider_stats={"status": "warning", "suggested_next_action": "check_provider"},
        extended_data_gates={
            "current_gate": "download_queue",
            "operator_wait_reason": "download_queue_active",
            "suggested_next_action": "continue_monitoring",
        },
        extended_data_readiness={"suggested_next_action": "wait_for_queue_then_rerun_audit"},
        tushare_retry_plan={"retry_gate": "wait_for_queue_completion", "suggested_next_action": "continue_monitoring"},
        extended_import_verification={
            "post_import_gate": "wait_for_download_completion",
            "suggested_next_action": "wait_for_queue_then_audit_and_import",
        },
    )

    assert suggested == "continue_monitoring"


def test_data_health_suggested_action_keeps_existing_freshness_priority_without_extended_gate() -> None:
    from scripts.run_data_health_dashboard import _choose_suggested_next_action

    suggested = _choose_suggested_next_action(
        freshness={"status": "warning", "suggested_next_action": "refresh_daily_data"},
        intraday={"status": "warning", "suggested_next_action": "refresh_intraday"},
        provider_stats={"status": "warning", "suggested_next_action": "check_provider"},
        extended_data_gates={},
        extended_data_readiness={"suggested_next_action": ""},
        tushare_retry_plan={"retry_gate": "no_retry_needed", "suggested_next_action": ""},
        extended_import_verification={
            "post_import_gate": "canonical_presence_verified",
            "suggested_next_action": "run_data_readiness_and_market_intel",
        },
    )

    assert suggested == "refresh_daily_data"


def test_data_health_suggested_action_surfaces_full_audit_before_import() -> None:
    from scripts.run_data_health_dashboard import _choose_suggested_next_action

    suggested = _choose_suggested_next_action(
        freshness={"status": "warning", "suggested_next_action": "refresh_daily_data"},
        intraday={"status": "warning", "suggested_next_action": "refresh_intraday"},
        provider_stats={"status": "warning", "suggested_next_action": "check_provider"},
        extended_data_gates={},
        extended_data_readiness={"suggested_next_action": FULL_AUDIT_COMMAND},
        tushare_retry_plan={"retry_gate": "no_retry_needed", "suggested_next_action": ""},
        extended_import_verification={
            "post_import_gate": "canonical_presence_verified",
            "suggested_next_action": "run_data_readiness_and_market_intel",
        },
    )

    assert suggested == FULL_AUDIT_COMMAND


def test_data_health_cli_summary_exposes_extended_data_gate() -> None:
    from quantdesk.cli_summary import build_cli_summary, format_human_summary

    summary = build_cli_summary(
        {
            "task_type": "data_health_dashboard",
            "status": "warning",
            "data_health_summary": {
                "extended_data_current_gate": "download_queue",
                "extended_data_gate_suggested_next_action": "continue_monitoring",
                "extended_data_queue_ok_count": 12,
                "extended_data_queue_fail_count": 1,
                "extended_data_queue_traceback_count": 0,
                "extended_data_queue_stdout_age_seconds": 5,
                "extended_data_queue_stderr_age_seconds": 7,
                "extended_data_queue_liveness": "active",
                "extended_data_queue_progress_state": "advancing",
                "extended_data_queue_ok_delta": 3,
                "extended_data_queue_fail_delta": 0,
                "extended_data_queue_traceback_delta": 0,
                "extended_data_operator_wait_reason": "download_queue_active",
                "extended_data_manual_review_required": False,
                "extended_data_manual_review_reason": "",
                "extended_import_domain_counts": {"events": 5, "fundamentals": 7},
                "extended_import_missing_planned_domains": ["moneyflow"],
                "extended_import_duplicate_destination_count": 2,
                "extended_import_existing_destination_count": 1,
                "extended_import_would_improve_domains": ["events", "fundamentals"],
                "extended_import_max_files_per_rule": 20,
                "extended_import_source_scan_limit_applied": True,
                "extended_import_source_scan_limited_domains": ["events"],
                "extended_import_full_audit_command": FULL_AUDIT_COMMAND,
                "extended_import_source_scan_full_audit_command": FULL_AUDIT_COMMAND,
                "extended_data_requires_user_confirmation": False,
                "extended_data_confirmation_required_command": "",
                "extended_data_safe_next_commands": [
                    "python scripts/qd.py tushare-queue-status --summary true",
                    "python scripts/qd.py data-gates --summary true",
                ],
                "extended_data_blocked_without_confirmation": [
                    "python scripts/qd.py import-extended-data --apply true",
                ],
                "tushare_minute_status": "running",
                "tushare_minute_symbol_done_count": 7,
                "tushare_minute_symbol_count": 2656,
                "tushare_minute_last_symbol": "920010",
                "tushare_minute_coverage_status": "download_in_progress",
                "tushare_minute_target_symbol_count": 2656,
                "tushare_minute_current_available_count": 7,
                "tushare_minute_canonical_current_available_count": 5,
                "tushare_minute_staged_current_not_imported_count": 2,
                "tushare_minute_missing_or_stale_count": 2649,
                "tushare_minute_current_available_ratio": 0.002635,
                "tushare_minute_coverage_suggested_next_action": "continue_monitoring_minute_download",
                "tushare_minute_latest_completed_quality_status": "passed",
                "tushare_minute_latest_completed_quality_total_symbols": 200,
                "tushare_minute_latest_completed_quality_usable_count": 200,
                "tushare_minute_latest_completed_quality_invalid_count": 0,
                "tushare_minute_latest_completed_quality_missing_count": 0,
                "tushare_minute_latest_completed_quality_target_date": "2026-07-03",
                "extended_import_preapply_gate": "wait_for_download_completion",
                "extended_import_gate": "wait_for_download_completion",
                "tushare_retry_gate": "wait_for_queue_completion",
            },
            "suggested_next_action": "continue_monitoring",
        }
    )

    assert summary["extended_data_current_gate"] == "download_queue"
    assert summary["extended_data_gate_suggested_next_action"] == "continue_monitoring"
    assert summary["extended_data_queue_ok_count"] == 12
    assert summary["extended_data_queue_fail_count"] == 1
    assert summary["extended_data_queue_traceback_count"] == 0
    assert summary["extended_data_queue_stdout_age_seconds"] == 5
    assert summary["extended_data_queue_stderr_age_seconds"] == 7
    assert summary["extended_data_queue_liveness"] == "active"
    assert summary["extended_data_queue_progress_state"] == "advancing"
    assert summary["extended_data_queue_ok_delta"] == 3
    assert summary["extended_data_queue_fail_delta"] == 0
    assert summary["extended_data_queue_traceback_delta"] == 0
    assert summary["extended_data_operator_wait_reason"] == "download_queue_active"
    assert summary["extended_data_manual_review_required"] is False
    assert summary["extended_data_manual_review_reason"] == ""
    assert summary["extended_data_requires_user_confirmation"] is False
    assert summary["extended_data_confirmation_required_command"] == ""
    assert summary["extended_import_domain_counts"] == {"events": 5, "fundamentals": 7}
    assert summary["extended_import_missing_planned_domains"] == ["moneyflow"]
    assert summary["extended_import_max_files_per_rule"] == 20
    assert summary["extended_import_source_scan_limit_applied"] is True
    assert summary["extended_import_source_scan_limited_domains"] == ["events"]
    assert summary["extended_import_full_audit_command"] == FULL_AUDIT_COMMAND
    assert summary["extended_import_source_scan_full_audit_command"] == FULL_AUDIT_COMMAND
    assert summary["tushare_minute_status"] == "running"
    assert summary["tushare_minute_symbol_done_count"] == 7
    assert summary["tushare_minute_symbol_count"] == 2656
    assert summary["tushare_minute_last_symbol"] == "920010"
    assert summary["tushare_minute_current_available_count"] == 7
    assert summary["tushare_minute_canonical_current_available_count"] == 5
    assert summary["tushare_minute_staged_current_not_imported_count"] == 2
    assert summary["tushare_minute_missing_or_stale_count"] == 2649
    assert summary["tushare_minute_latest_completed_quality_status"] == "passed"
    assert summary["tushare_minute_latest_completed_quality_usable_count"] == 200
    assert summary["tushare_minute_latest_completed_quality_total_symbols"] == 200
    assert "python scripts/qd.py data-gates --summary true" in summary["extended_data_safe_next_commands"]
    assert "python scripts/qd.py import-extended-data --apply true" in summary["extended_data_blocked_without_confirmation"]
    assert summary["key_counts"]["extended_data_current_gate"] == "download_queue"
    assert summary["key_counts"]["extended_data_queue_ok_count"] == 12
    assert summary["key_counts"]["extended_data_queue_progress_state"] == "advancing"
    assert summary["key_counts"]["tushare_minute_symbol_done_count"] == 7
    assert summary["key_counts"]["tushare_minute_missing_or_stale_count"] == 2649
    assert "Extended data gate: download_queue" in format_human_summary(summary)
    assert "Extended data progress: advancing" in format_human_summary(summary)
    assert "Extended staged domains: events=5, fundamentals=7" in format_human_summary(summary)
    assert "Extended staged counts bounded: max_files_per_rule=20" in format_human_summary(summary)
    assert f"Extended staged full audit: {FULL_AUDIT_COMMAND}" in format_human_summary(summary)
    assert (
        "Tushare minute pool: downloaded=7/2656, available=7/2656, "
        "canonical=5, staged_pending_import=2, missing_or_stale=2649, last=920010"
    ) in format_human_summary(summary)
    assert "Tushare completed minute quality: status=passed, usable=200/200" in format_human_summary(summary)
