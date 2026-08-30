from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _write(path: Path, text: str = "ts_code,trade_date,value\n000001.SZ,20260703,1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_staging(root: Path) -> Path:
    staging = root / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310"
    _write(staging / "index_sectors" / "raw" / "index_daily" / "000001.SH.csv")
    _write(staging / "index_sectors" / "raw" / "index_weight" / "000300.SH" / "20260101_20261231.csv")
    _write(staging / "index_sectors" / "raw" / "ths" / "ths_index.csv", "ts_code,name\n700001.TI,test theme\n")
    _write(staging / "index_sectors" / "raw" / "ths" / "ths_member" / "700001.TI.csv")
    _write(staging / "index_sectors" / "raw" / "ths" / "ths_daily" / "700001.TI.csv")
    _write(staging / "moneyflow" / "raw" / "by_trade_date" / "moneyflow" / "20260703.csv")
    _write(staging / "moneyflow" / "raw" / "by_trade_date" / "moneyflow_hsgt" / "20260703.csv")
    _write(staging / "events" / "raw" / "by_trade_date" / "top_list" / "20260703.csv")
    _write(staging / "events" / "raw" / "by_trade_date" / "top_inst" / "20260703.csv")
    _write(staging / "events" / "raw" / "by_trade_date" / "margin" / "20260703.csv")
    _write(staging / "events" / "raw" / "by_trade_date" / "margin_detail" / "20260703.csv")
    _write(staging / "events" / "raw" / "by_trade_date" / "block_trade" / "20260703.csv")
    _write(staging / "events" / "raw" / "by_trade_date" / "limit_list_d" / "20260703.csv")
    _write(staging / "fundamentals" / "raw" / "by_symbol" / "income" / "000001.SZ.csv")
    _write(staging / "fundamentals" / "raw" / "by_symbol" / "balancesheet" / "000001.SZ.csv")
    _write(staging / "fundamentals" / "raw" / "by_symbol" / "cashflow" / "000001.SZ.csv")
    _write(staging / "fundamentals" / "raw" / "by_symbol" / "fina_indicator" / "000001.SZ.csv")
    _write(staging / "fundamentals" / "raw" / "by_symbol" / "dividend" / "000001.SZ.csv")
    _write(staging / "fundamentals" / "raw" / "by_symbol" / "forecast" / "000001.SZ.csv")
    _write(staging / "fundamentals" / "raw" / "by_symbol" / "express" / "000001.SZ.csv")
    _write(staging / "fundamentals" / "raw" / "stock_basic" / "stock_basic.csv")
    _write(staging / "minute_partial_retry" / "raw" / "1min" / "000001.csv", "trade_time,open,close\n2026-07-03 09:31:00,1,1\n")
    return staging


def test_extended_staged_importer_dry_run_does_not_write_canonical_data(tmp_path: Path) -> None:
    from quantdesk.extended_staged_data_importer import build_extended_staged_import_plan

    _make_staging(tmp_path)

    payload = build_extended_staged_import_plan(project_root=tmp_path, apply=False)

    assert payload["task_type"] == "extended_staged_data_import"
    assert payload["dry_run"] is True
    assert payload["planned_file_count"] >= 6
    assert payload["copied_file_count"] == 0
    assert payload["duplicate_destination_count"] == 0
    assert not (tmp_path / "data" / "sector" / "index" / "000001.SH.csv").exists()
    assert any(action["domain"] == "moneyflow" for action in payload["actions"])
    assert payload["domain_counts"]["events"] >= 6
    assert payload["domain_counts"]["fundamentals"] >= 7
    assert payload["domain_counts"]["intraday_1m"] == 1
    assert payload["source_scan_limit_applied"] is False


def test_extended_staged_importer_marks_source_scan_limit(tmp_path: Path) -> None:
    from quantdesk.extended_staged_data_importer import FULL_AUDIT_COMMAND, build_extended_staged_import_plan

    staging = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310"
    for index in range(3):
        _write(staging / "events" / "raw" / "by_trade_date" / "top_list" / f"2026070{index + 1}.csv")

    payload = build_extended_staged_import_plan(project_root=tmp_path, apply=False, max_files_per_rule=2)

    assert payload["planned_file_count"] == 2
    assert payload["domain_counts"]["events"] == 2
    assert payload["max_files_per_rule"] == 2
    assert payload["source_scan_limit_applied"] is True
    assert payload["source_scan_limited_domains"] == ["events"]
    assert payload["source_scan_limited_rule_count"] == 1
    assert payload["full_audit_command"] == FULL_AUDIT_COMMAND
    assert payload["source_scan_full_audit_command"] == FULL_AUDIT_COMMAND


def test_extended_staged_importer_default_scan_is_unbounded(tmp_path: Path) -> None:
    from quantdesk.extended_staged_data_importer import build_extended_staged_import_plan

    staging = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310"
    for index in range(3):
        _write(staging / "events" / "raw" / "by_trade_date" / "top_list" / f"2026070{index + 1}.csv")

    payload = build_extended_staged_import_plan(project_root=tmp_path, apply=False)

    assert payload["planned_file_count"] == 3
    assert payload["domain_counts"]["events"] == 3
    assert payload["max_files_per_rule"] == 0
    assert payload["source_scan_limit_applied"] is False
    assert payload["source_scan_full_audit_command"] == ""


def test_extended_staged_importer_apply_copies_to_canonical_dirs(tmp_path: Path) -> None:
    from quantdesk.extended_data_readiness import build_extended_data_readiness
    from quantdesk.extended_staged_data_importer import build_extended_staged_import_plan

    _make_staging(tmp_path)

    payload = build_extended_staged_import_plan(project_root=tmp_path, apply=True)

    assert payload["dry_run"] is False
    assert payload["copied_file_count"] >= 6
    assert (tmp_path / "data" / "sector" / "index" / "000001.SH.csv").exists()
    assert (tmp_path / "data" / "sector" / "members" / "ths_member_700001.TI.csv").exists()
    assert (tmp_path / "data" / "sector" / "members" / "index_weight" / "000300.SH" / "20260101_20261231.csv").exists()
    assert (tmp_path / "data" / "theme" / "ths_index.csv").exists()
    assert (tmp_path / "data" / "moneyflow" / "moneyflow" / "20260703.csv").exists()
    assert (tmp_path / "data" / "moneyflow" / "moneyflow_hsgt" / "20260703.csv").exists()
    assert (tmp_path / "data" / "events" / "top_list" / "20260703.csv").exists()
    assert (tmp_path / "data" / "events" / "top_inst" / "20260703.csv").exists()
    assert (tmp_path / "data" / "events" / "margin" / "20260703.csv").exists()
    assert (tmp_path / "data" / "events" / "margin_detail" / "20260703.csv").exists()
    assert (tmp_path / "data" / "events" / "block_trade" / "20260703.csv").exists()
    assert (tmp_path / "data" / "events" / "limit_list_d" / "20260703.csv").exists()
    assert (tmp_path / "data" / "fundamentals" / "income" / "000001.SZ.csv").exists()
    assert (tmp_path / "data" / "fundamentals" / "balancesheet" / "000001.SZ.csv").exists()
    assert (tmp_path / "data" / "fundamentals" / "cashflow" / "000001.SZ.csv").exists()
    assert (tmp_path / "data" / "fundamentals" / "fina_indicator" / "000001.SZ.csv").exists()
    assert (tmp_path / "data" / "fundamentals" / "stock_basic" / "stock_basic.csv").exists()
    assert not (tmp_path / "data" / "intraday" / "1m" / "000001.csv").exists()
    assert next(
        action for action in payload["actions"] if action["domain"] == "intraday_1m"
    )["status"] == "blocked_requires_dedicated_intraday_importer"

    readiness = build_extended_data_readiness(project_root=tmp_path)
    assert readiness["domains"]["sector_index"]["status"] in {"partial", "available"}
    assert readiness["domains"]["sector_membership"]["status"] in {"partial", "available"}
    assert readiness["domains"]["moneyflow"]["status"] in {"partial", "available"}
    assert readiness["domains"]["events"]["status"] in {"partial", "available"}
    assert readiness["domains"]["intraday_1m"]["status"] == "staged_not_imported"


def test_extended_staged_importer_never_copies_intraday_into_canonical(tmp_path: Path) -> None:
    """Minute files must use the dedicated audited Stage 6 importer."""

    from quantdesk.extended_staged_data_importer import build_extended_staged_import_plan

    _make_staging(tmp_path)

    payload = build_extended_staged_import_plan(project_root=tmp_path, apply=True)

    minute_actions = [
        action for action in payload["actions"] if action["domain"] == "intraday_1m"
    ]
    assert len(minute_actions) == 1
    assert minute_actions[0]["status"] == "blocked_requires_dedicated_intraday_importer"
    assert not (tmp_path / "data" / "intraday" / "1m" / "000001.csv").exists()
    assert (tmp_path / "data" / "events" / "top_list" / "20260703.csv").exists()


def test_extended_staged_importer_apply_blocks_while_queue_is_running(tmp_path: Path) -> None:
    from quantdesk.extended_staged_data_importer import build_extended_staged_import_plan

    staging = _make_staging(tmp_path)
    (staging / "queue_status.json").write_text(
        json.dumps({"status": "running", "stages": [{"key": "harvest_index_sectors", "status": "starting"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    payload = build_extended_staged_import_plan(project_root=tmp_path, apply=True)

    assert payload["status"] == "blocked"
    assert payload["apply"] is False
    assert payload["blocked_reason"] == "download_queue_still_running"
    assert payload["copied_file_count"] == 0
    assert payload["suggested_next_action"] == "wait_for_queue_then_rerun_audit"
    assert not (tmp_path / "data" / "sector" / "index" / "000001.SH.csv").exists()


def test_extended_staged_importer_full_dry_run_short_circuits_while_queue_running(tmp_path: Path, monkeypatch) -> None:
    from quantdesk import extended_staged_data_importer as importer

    staging = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310"
    staging.mkdir(parents=True)
    (staging / "queue_status.json").write_text(
        json.dumps({"status": "running", "current_stage": "harvest_fundamentals"}, ensure_ascii=False),
        encoding="utf-8",
    )

    def fail_if_scanned(*_: object, **__: object) -> tuple[list[Path], bool]:
        raise AssertionError("full dry-run import plan must not scan staged files while queue is running")

    monkeypatch.setattr(importer, "_iter_sources", fail_if_scanned)

    payload = importer.build_extended_staged_import_plan(project_root=tmp_path, apply=False, max_files_per_rule=0)

    assert payload["status"] == "blocked"
    assert payload["dry_run"] is True
    assert payload["apply"] is False
    assert payload["blocked_reason"] == "download_queue_still_running"
    assert payload["planned_file_count"] == 0
    assert payload["domain_counts"] == {}
    assert payload["suggested_next_action"] == "wait_for_queue_then_rerun_audit"


def test_extended_staged_importer_cli_is_dry_run_by_default(tmp_path: Path) -> None:
    _make_staging(tmp_path)
    out = tmp_path / "import_plan.json"

    completed = subprocess.run(
        [
            PYTHON,
            str(PROJECT_ROOT / "scripts" / "import_extended_staged_data.py"),
            "--project-root",
            str(tmp_path),
            "--output",
            str(out),
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
    assert payload["dry_run"] is True
    assert payload["max_files_per_rule"] == 0
    assert out.exists()
    assert not (tmp_path / "data" / "moneyflow" / "moneyflow" / "20260703.csv").exists()

    help_text = subprocess.check_output(
        [PYTHON, str(PROJECT_ROOT / "scripts" / "qd.py"), "help"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert "import-extended-data" in help_text


def test_extended_staged_import_audit_flags_duplicates_and_existing_destinations(tmp_path: Path) -> None:
    from quantdesk.extended_staged_data_importer import build_extended_staged_import_audit

    _make_staging(tmp_path)
    duplicate_staging = tmp_path / "data" / "vendor_staging" / "tushare_proxy_extended_full_20260705_010203"
    _write(duplicate_staging / "sectors" / "raw" / "ths" / "ths_index.csv", "ts_code,name\n700001.TI,duplicate theme\n")
    _write(tmp_path / "data" / "moneyflow" / "moneyflow" / "20260703.csv", "old,value\n1,1\n")

    payload = build_extended_staged_import_audit(project_root=tmp_path, max_files_per_rule=20)

    assert payload["task_type"] == "extended_staged_import_audit"
    assert payload["status"] == "warning"
    assert payload["planned_file_count"] > 0
    assert payload["duplicate_destination_count"] == 1
    assert payload["existing_destination_count"] >= 1
    assert payload["preapply_gate"] == "review_required"
    assert "concept_theme" in payload["domain_counts"]
    assert any("duplicate_destination_paths" in warning for warning in payload["warnings"])


def test_extended_staged_import_audit_prioritizes_running_queue_gate(tmp_path: Path) -> None:
    from quantdesk.extended_staged_data_importer import FULL_AUDIT_COMMAND, build_extended_staged_import_audit

    staging = _make_staging(tmp_path)
    report = tmp_path / "audit_running_queue.md"
    (staging / "queue_status.json").write_text(
        json.dumps(
            {
                "status": "running",
                "stages": [{"key": "harvest_index_sectors", "status": "starting"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    duplicate_staging = tmp_path / "data" / "vendor_staging" / "tushare_proxy_extended_full_20260705_010203"
    _write(duplicate_staging / "sectors" / "raw" / "ths" / "ths_index.csv", "ts_code,name\n700001.TI,duplicate theme\n")
    _write(tmp_path / "data" / "moneyflow" / "moneyflow" / "20260703.csv", "old,value\n1,1\n")

    payload = build_extended_staged_import_audit(project_root=tmp_path, max_files_per_rule=20, report_path=report)

    assert payload["preapply_gate"] == "wait_for_download_completion"
    assert payload["canonical_writes_allowed"] is False
    assert payload["apply_command_after_review"] == ""
    assert payload["suggested_next_action"] == "wait_for_queue_then_rerun_audit"
    assert payload["download_queue"]["current_stage"] == "harvest_index_sectors"
    assert "download_queue_still_running" in payload["warnings"]
    report_text = report.read_text(encoding="utf-8")
    assert "Download Queue" in report_text
    assert "harvest_index_sectors" in report_text
    assert "wait_for_queue_then_rerun_audit" in report_text

    cli_completed = subprocess.run(
        [
            PYTHON,
            str(PROJECT_ROOT / "scripts" / "audit_extended_staged_import.py"),
            "--project-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "audit_running_queue.json"),
            "--report",
            str(tmp_path / "audit_running_queue_cli.md"),
            "--summary",
            "true",
            "--max-files-per-rule",
            "20",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    assert cli_completed.returncode == 0, cli_completed.stderr
    assert "queue_stage: harvest_index_sectors" in cli_completed.stdout
    assert "suggested_next_action: wait_for_queue_then_rerun_audit" in cli_completed.stdout
    assert f"full_audit_command: {FULL_AUDIT_COMMAND}" in cli_completed.stdout


def test_extended_staged_import_audit_marks_stale_running_queue_for_manual_review(
    tmp_path: Path, monkeypatch
) -> None:
    from quantdesk import tushare_queue_status
    from quantdesk.extended_staged_data_importer import build_extended_staged_import_audit

    _make_staging(tmp_path)
    queue = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_030405"
    queue.mkdir(parents=True)
    (queue / "queue_status.json").write_text(
        json.dumps(
            {
                "status": "running",
                "current_stage": "harvest_fundamentals",
                "current_stage_status": "running",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stdout = queue / "harvest_fundamentals_stdout.log"
    stdout.write_text("OK old progress\n", encoding="utf-8")
    old_timestamp = time.time() - 3600
    os.utime(stdout, (old_timestamp, old_timestamp))

    monkeypatch.setattr(
        tushare_queue_status,
        "_process_counts",
        lambda: {
            "checked": True,
            "counts": {
                "tushare_continuous_data_queue.py": 0,
                "tushare_proxy_extended_harvest.py": 0,
                "tushare_minute_probe_and_download.py": 0,
            },
            "error": "",
        },
    )
    report = tmp_path / "audit_stale_queue.md"

    payload = build_extended_staged_import_audit(project_root=tmp_path, max_files_per_rule=20, report_path=report)

    assert payload["preapply_gate"] == "wait_for_download_completion_queue_stale_manual_review"
    assert payload["suggested_next_action"] == "review_queue_state_then_restart_or_confirm"
    assert payload["canonical_writes_allowed"] is False
    assert payload["apply_command_after_review"] == ""
    assert payload["queue_liveness"] == "stale"
    assert payload["queue_process_found"] is False
    assert payload["download_queue"]["current_stage"] == "harvest_fundamentals"
    assert "download_queue_stale_manual_review_required" in payload["warnings"]
    report_text = report.read_text(encoding="utf-8")
    assert "queue_liveness: stale" in report_text
    assert "Queue appears stalled" in report_text


def test_extended_staged_import_audit_cli_outputs_json_and_report(tmp_path: Path) -> None:
    from quantdesk.extended_staged_data_importer import FULL_AUDIT_COMMAND

    _make_staging(tmp_path)
    out = tmp_path / "audit.json"
    report = tmp_path / "audit.md"

    completed = subprocess.run(
        [
            PYTHON,
            str(PROJECT_ROOT / "scripts" / "audit_extended_staged_import.py"),
            "--project-root",
            str(tmp_path),
            "--output",
            str(out),
            "--report",
            str(report),
            "--json",
            "true",
            "--max-files-per-rule",
            "20",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["task_type"] == "extended_staged_import_audit"
    assert payload["full_audit_command"] == FULL_AUDIT_COMMAND
    assert out.exists()
    assert report.exists()
    assert "Extended Staged Import Audit" in report.read_text(encoding="utf-8")

    help_text = subprocess.check_output(
        [PYTHON, str(PROJECT_ROOT / "scripts" / "qd.py"), "help"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert "audit-extended-import" in help_text


def test_extended_staged_import_audit_cli_default_scan_is_unbounded(tmp_path: Path) -> None:
    _make_staging(tmp_path)

    completed = subprocess.run(
        [
            PYTHON,
            str(PROJECT_ROOT / "scripts" / "audit_extended_staged_import.py"),
            "--project-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "audit_default.json"),
            "--report",
            str(tmp_path / "audit_default.md"),
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
    assert payload["max_files_per_rule"] == 0
    assert payload["source_scan_limit_applied"] is False


def test_extended_staged_import_full_audit_short_circuits_while_queue_running(tmp_path: Path, monkeypatch) -> None:
    from quantdesk import extended_staged_data_importer as importer

    queue = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_020304"
    queue.mkdir(parents=True)
    (queue / "queue_status.json").write_text(
        json.dumps({"status": "running", "current_stage": "harvest_fundamentals"}, ensure_ascii=False),
        encoding="utf-8",
    )

    def fail_if_scanned(**_: object) -> dict[str, object]:
        raise AssertionError("full audit must not scan staged files while queue is running")

    monkeypatch.setattr(importer, "build_extended_staged_import_plan", fail_if_scanned)

    payload = importer.build_extended_staged_import_audit(project_root=tmp_path, max_files_per_rule=0)

    assert payload["preapply_gate"] == "wait_for_download_completion"
    assert payload["suggested_next_action"] == "wait_for_queue_then_rerun_audit"
    assert payload["planned_file_count"] == 0
    assert payload["domain_counts"] == {}


def test_extended_staged_import_audit_completed_queue_is_not_running(tmp_path: Path) -> None:
    from quantdesk.extended_staged_data_importer import FULL_APPLY_COMMAND, build_extended_staged_import_audit

    staging = tmp_path / "data" / "vendor_staging" / "tushare_proxy_extended_full_20260705_010203"
    _write(staging / "events" / "raw" / "by_trade_date" / "top_list" / "20260703.csv")
    queue = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_020304"
    queue.mkdir(parents=True)
    (queue / "queue_status.json").write_text(
        json.dumps({"status": "completed", "current_stage": "harvest_moneyflow"}, ensure_ascii=False),
        encoding="utf-8",
    )

    payload = build_extended_staged_import_audit(project_root=tmp_path, max_files_per_rule=0)

    assert payload["download_queue"]["status"] == "completed"
    assert payload["preapply_gate"] == "ready_for_full_apply"
    assert payload["suggested_next_action"] == "confirm_full_apply_with_user"
    assert payload["apply_command_after_review"] == FULL_APPLY_COMMAND


def test_extended_staged_import_audit_distinguishes_full_and_bounded_ready_gates(tmp_path: Path) -> None:
    from quantdesk.extended_staged_data_importer import FULL_APPLY_COMMAND, build_extended_staged_import_audit

    staging = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310"
    for index in range(3):
        _write(staging / "events" / "raw" / "by_trade_date" / "top_list" / f"2026070{index + 1}.csv")

    full_payload = build_extended_staged_import_audit(project_root=tmp_path, max_files_per_rule=0)

    assert full_payload["preapply_gate"] == "ready_for_full_apply"
    assert full_payload["suggested_next_action"] == "confirm_full_apply_with_user"
    assert full_payload["apply_command_after_review"] == FULL_APPLY_COMMAND
    assert full_payload["source_scan_limit_applied"] is False

    bounded_payload = build_extended_staged_import_audit(project_root=tmp_path, max_files_per_rule=2)

    assert bounded_payload["preapply_gate"] == "ready_for_bounded_apply"
    assert bounded_payload["suggested_next_action"] == "confirm_bounded_apply_with_user"
    assert bounded_payload["apply_command_after_review"] == FULL_APPLY_COMMAND
    assert bounded_payload["source_scan_limit_applied"] is True
