from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def test_tushare_queue_status_reads_latest_queue_and_redacts_log_tail(tmp_path: Path) -> None:
    from quantdesk.tushare_queue_status import build_tushare_queue_status

    queue = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310"
    logs = queue / "logs"
    logs.mkdir(parents=True)
    (queue / "queue_status.json").write_text(
        json.dumps({"status": "running", "current_stage": "harvest_index_sectors"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (logs / "harvest_index_sectors_stdout.log").write_text(
        "\n".join(
            [
                "OK ths_daily/885001.TI rows=10",
                "OK ths_member/885001.TI rows=5 token=1234567890abcdef1234567890abcdef",
            ]
        ),
        encoding="utf-8",
    )
    (logs / "harvest_index_sectors_stderr.log").write_text(
        "FAIL ths_daily/885002.TI SSL: UNEXPECTED_EOF while reading\n",
        encoding="utf-8",
    )

    payload = build_tushare_queue_status(project_root=tmp_path, include_processes=False, stale_after_seconds=3600)

    assert payload["task_type"] == "tushare_queue_status"
    assert payload["status"] == "running"
    assert payload["queue"]["current_stage"] == "harvest_index_sectors"
    assert payload["log_summary"]["ok_count"] == 2
    assert payload["log_summary"]["fail_count"] == 1
    assert payload["log_summary"]["ssl_eof_count"] == 1
    assert payload["processes"]["checked"] is False
    assert payload["suggested_next_action"] == "continue_monitoring"
    text = json.dumps(payload, ensure_ascii=False)
    assert "1234567890abcdef1234567890abcdef" not in text
    assert "<redacted" in text


def test_tushare_queue_status_missing_queue_is_explicit(tmp_path: Path) -> None:
    from quantdesk.tushare_queue_status import build_tushare_queue_status

    payload = build_tushare_queue_status(project_root=tmp_path, include_processes=False)

    assert payload["status"] == "missing"
    assert payload["queue"]["path"] == ""
    assert payload["suggested_next_action"] == "no_queue_found"
    assert any("missing:tushare_queue" in warning for warning in payload["warnings"])


def test_tushare_queue_status_derives_current_stage_from_stage_history(tmp_path: Path) -> None:
    from quantdesk.tushare_queue_status import build_tushare_queue_status

    queue = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310"
    logs = queue / "logs"
    logs.mkdir(parents=True)
    (queue / "queue_status.json").write_text(
        json.dumps(
            {
                "status": "running",
                "stages": [
                    {"key": "intraday_coverage_audit", "status": "completed"},
                    {"key": "harvest_index_sectors", "status": "starting"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (logs / "harvest_index_sectors_stdout.log").write_text("OK ths_index rows=1\n", encoding="utf-8")

    payload = build_tushare_queue_status(project_root=tmp_path, include_processes=False, stale_after_seconds=3600)

    assert payload["queue"]["current_stage"] == "harvest_index_sectors"
    assert payload["queue"]["current_stage_status"] == "starting"
    assert payload["current_logs"]["stdout"]["stage"] == "harvest_index_sectors"


def test_tushare_queue_status_exposes_stage_progress_from_requested_stages(tmp_path: Path) -> None:
    from quantdesk.tushare_queue_status import build_tushare_queue_status, render_tushare_queue_status_report

    queue = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310"
    logs = queue / "logs"
    logs.mkdir(parents=True)
    (queue / "queue_status.json").write_text(
        json.dumps(
            {
                "status": "running",
                "stages_requested": [
                    {"key": "minute_retry_partial", "label": "Retry partial minute symbols"},
                    {"key": "intraday_coverage_audit", "label": "Audit intraday coverage"},
                    {"key": "harvest_fundamentals", "label": "Harvest fundamentals"},
                    {"key": "harvest_moneyflow", "label": "Harvest moneyflow"},
                ],
                "stages": [
                    {"key": "minute_retry_partial", "status": "completed"},
                    {"key": "intraday_coverage_audit", "status": "skipped_no_live_process_found_existing_events_dir"},
                    {"key": "harvest_fundamentals", "status": "starting"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (logs / "harvest_fundamentals_stdout.log").write_text("OK income/000001.SZ rows=1\n", encoding="utf-8")

    payload = build_tushare_queue_status(project_root=tmp_path, include_processes=False, stale_after_seconds=3600)
    progress = payload["queue"]["stage_progress"]

    assert progress["requested_stage_count"] == 4
    assert progress["completed_stage_count"] == 2
    assert progress["current_stage"] == "harvest_fundamentals"
    assert progress["current_stage_number"] == 3
    assert progress["stage_position_label"] == "3/4"
    assert progress["remaining_stage_count"] == 1
    assert progress["remaining_stage_keys"] == ["harvest_moneyflow"]
    assert payload["stage_progress"] == progress
    report = render_tushare_queue_status_report(payload)
    assert "Stage progress: `3/4`" in report


def test_tushare_queue_status_counts_command_finished_as_completed_stage(tmp_path: Path) -> None:
    from quantdesk.tushare_queue_status import build_tushare_queue_status

    queue = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310"
    logs = queue / "logs"
    logs.mkdir(parents=True)
    (queue / "queue_status.json").write_text(
        json.dumps(
            {
                "status": "running",
                "stages_requested": [
                    {"key": "minute_retry_partial"},
                    {"key": "intraday_coverage_audit"},
                    {"key": "harvest_index_sectors"},
                    {"key": "harvest_fundamentals"},
                    {"key": "harvest_moneyflow"},
                ],
                "stages": [
                    {"key": "minute_retry_partial", "status": "command_finished", "updated_at": "2000-01-01T00:00:00+00:00"},
                    {"key": "intraday_coverage_audit", "status": "completed", "updated_at": "2000-01-01T00:05:00+00:00"},
                    {"key": "harvest_index_sectors", "status": "command_finished", "updated_at": "2000-01-01T00:10:00+00:00"},
                    {"key": "harvest_fundamentals", "status": "starting", "updated_at": "2000-01-01T00:15:00+00:00"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (logs / "harvest_fundamentals_stdout.log").write_text("OK income/000001.SZ rows=1\n", encoding="utf-8")

    payload = build_tushare_queue_status(project_root=tmp_path, include_processes=False, stale_after_seconds=3600)
    progress = payload["queue"]["stage_progress"]

    assert progress["stage_position_label"] == "4/5"
    assert progress["completed_stage_count"] == 3
    assert progress["completed_stage_keys"] == [
        "minute_retry_partial",
        "intraday_coverage_audit",
        "harvest_index_sectors",
    ]
    assert progress["remaining_stage_keys"] == ["harvest_moneyflow"]
    assert progress["current_stage_started_at"] == "2000-01-01T00:15:00+00:00"
    assert progress["current_stage_last_event_status"] == "starting"
    assert progress["current_stage_elapsed_seconds"] > 0
    assert progress["last_stage_event_key"] == "harvest_fundamentals"
    assert progress["last_stage_event_age_seconds"] > 0


def test_tushare_queue_status_uses_resume_log_for_current_stage(tmp_path: Path) -> None:
    from quantdesk.tushare_queue_status import build_tushare_queue_status

    queue = tmp_path / "data" / "vendor_staging" / "tushare_continuous_queue_20260705_024310"
    logs = queue / "logs"
    logs.mkdir(parents=True)
    (queue / "queue_status.json").write_text(
        json.dumps(
            {
                "status": "running",
                "current_stage": "harvest_fundamentals",
                "stages_requested": [{"key": "harvest_fundamentals"}, {"key": "harvest_moneyflow"}],
                "stages": [{"key": "harvest_fundamentals", "status": "starting"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    old_log = logs / "harvest_fundamentals_stdout.log"
    old_log.write_text("OK income/000001.SZ rows=1\n", encoding="utf-8")
    resume_log = logs / "harvest_fundamentals_resume_stdout.log"
    resume_log.write_text("OK income/600589.SH rows=1\n", encoding="utf-8")

    payload = build_tushare_queue_status(project_root=tmp_path, include_processes=False, stale_after_seconds=3600)

    assert payload["current_logs"]["stdout"]["path"].endswith("harvest_fundamentals_resume_stdout.log")
    assert payload["suggested_next_action"] == "continue_monitoring"


def test_tushare_queue_status_process_check_ignores_own_powershell_query(monkeypatch) -> None:
    from quantdesk import tushare_queue_status

    def fake_run(*args, **kwargs):  # noqa: ANN001, ANN202 - local subprocess stub
        command = args[0][-1]
        assert "$_.ProcessId -ne $PID" in command
        rows = [
            {
                "ProcessId": 111,
                "ParentProcessId": 1,
                "Name": "powershell.exe",
                "CommandLine": (
                    "powershell.exe -Command Get-CimInstance Win32_Process | "
                    "Where-Object { $_.CommandLine -match 'tushare_proxy_extended_harvest.py' "
                    "-or $_.CommandLine -match 'tushare_proxy_minutes_250d' }"
                ),
            },
            {
                "ProcessId": 222,
                "ParentProcessId": 1,
                "Name": "python.exe",
                "CommandLine": (
                    "python scripts\\tushare_proxy_extended_harvest.py "
                    "--groups fundamentals --out-dir data\\vendor_staging\\queue\\fundamentals"
                ),
            },
        ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(tushare_queue_status.subprocess, "run", fake_run)

    processes = tushare_queue_status._process_counts()

    assert processes["checked"] is True
    assert processes["counts"]["tushare_proxy_extended_harvest.py"] == 1
    assert processes["counts"]["tushare_minute_probe_and_download.py"] == 0


def test_tushare_queue_status_does_not_treat_minute_process_as_extended_stage_alive() -> None:
    from quantdesk.tushare_queue_status import _suggest_action

    process_info = {
        "counts": {
            "tushare_continuous_data_queue.py": 0,
            "tushare_proxy_extended_harvest.py": 0,
            "tushare_minute_probe_and_download.py": 1,
        }
    }
    stale_logs = {"stdout": {"age_seconds": 3600}}

    assert (
        _suggest_action(
            queue_status="running",
            current_stage="harvest_fundamentals",
            process_info=process_info,
            current_logs=stale_logs,
            totals={"traceback_count": 0},
            stale_after_seconds=900,
        )
        == "restart_queue_after_user_approval"
    )
    assert (
        _suggest_action(
            queue_status="running",
            current_stage="minute_retry_partial",
            process_info=process_info,
            current_logs=stale_logs,
            totals={"traceback_count": 0},
            stale_after_seconds=900,
        )
        == "continue_monitoring"
    )


def test_tushare_queue_status_cli_and_qd_help_contract(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            PYTHON,
            str(PROJECT_ROOT / "scripts" / "show_tushare_queue_status.py"),
            "--project-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "queue_status.json"),
            "--report",
            str(tmp_path / "queue_status.md"),
            "--json",
            "true",
            "--no-process-check",
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
    assert payload["task_type"] == "tushare_queue_status"
    assert (tmp_path / "queue_status.json").exists()
    assert (tmp_path / "queue_status.md").exists()

    help_text = subprocess.check_output(
        [PYTHON, str(PROJECT_ROOT / "scripts" / "qd.py"), "help"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert "tushare-queue-status" in help_text
