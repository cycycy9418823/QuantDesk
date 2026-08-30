from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from types import SimpleNamespace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def test_tushare_minute_status_detects_waiting_for_secure_token(tmp_path: Path) -> None:
    from quantdesk.tushare_minute_status import build_tushare_minute_status

    job = tmp_path / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_project_real_2656_20260705_184207"
    job.mkdir(parents=True)
    (job / "preflight.log").write_text(
        "\n".join(
            [
                "[2026-07-05T18:42:10] python=python",
                "[2026-07-05T18:42:10] pool_exists=True",
                "[2026-07-05T18:42:10] pool_symbols=2656",
            ]
        ),
        encoding="utf-8",
    )

    payload = build_tushare_minute_status(project_root=tmp_path, include_processes=False)

    assert payload["task_type"] == "tushare_minute_status"
    assert payload["status"] == "waiting_for_secure_token_input"
    assert payload["suggested_next_action"] == "paste_token_in_secure_window"
    assert payload["log_summary"]["symbol_done_count"] == 0
    assert payload["read_only"] is True
    assert payload["token_output"] is False


def test_tushare_minute_status_counts_progress_and_redacts(tmp_path: Path) -> None:
    from quantdesk.tushare_minute_status import build_tushare_minute_status, render_tushare_minute_status_report

    job = tmp_path / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_project_real_2656_20260705_184207"
    job.mkdir(parents=True)
    (job / "preflight.log").write_text("[2026-07-05T18:42:10] starting python downloader\n", encoding="utf-8")
    (job / "stdout.log").write_text(
        "\n".join(
            [
                "[2026-07-05T18:38:13] PROBE symbol=688403 date=20250624 ok=True rows=241 token=1234567890abcdef1234567890abcdef",
                "[2026-07-05T18:40:20] OK symbol=688403 chunk=20250624..20250721 rows=4820",
                "[2026-07-05T18:45:43] SYMBOL_DONE 1/2656 symbol=688403 rows=60248 status=ok after_rows=60248",
            ]
        ),
        encoding="utf-8",
    )
    (job / "summary.json").write_text(
        json.dumps(
            {
                "status": "running",
                "symbol_count": 2656,
                "target_end": "20260703",
                "days": 250,
                "download": [{"symbol": "688403", "status": "ok", "rows": 60248}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = build_tushare_minute_status(project_root=tmp_path, include_processes=False)
    text = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == "running"
    assert payload["suggested_next_action"] == "continue_monitoring"
    assert payload["log_summary"]["probe_count"] == 1
    assert payload["log_summary"]["ok_chunk_count"] == 1
    assert payload["log_summary"]["symbol_done_count"] == 1
    assert payload["log_summary"]["permission_count"] == 0
    assert payload["log_summary"]["last_symbol_done"]["symbol"] == "688403"
    assert "1234567890abcdef1234567890abcdef" not in text
    assert "<redacted" in text
    report = render_tushare_minute_status_report(payload)
    assert "symbol_done" in report


def test_tushare_minute_status_detects_minute_remaining_child_as_running(monkeypatch, tmp_path: Path) -> None:
    from quantdesk import tushare_minute_status

    job = tmp_path / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_project_real_2656_resume_20260706_114930"
    job.mkdir(parents=True)
    (job / "preflight.log").write_text(
        "[2026-07-06T11:49:30] START minute remaining child symbols=2495 outDir=x\n",
        encoding="utf-8",
    )
    (job / "summary.json").write_text(
        json.dumps({"status": "started", "symbol_count": 2495, "target_end": "20260703", "download": []}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        tushare_minute_status,
        "_process_matches",
        lambda _: [{"pid": 123, "name": "python.exe", "command_line": "python .tmp/tushare_minute_probe_and_download.py"}],
    )

    payload = tushare_minute_status.build_tushare_minute_status(project_root=tmp_path, include_processes=True)

    assert payload["status"] == "running"
    assert payload["suggested_next_action"] == "continue_monitoring"


def test_tushare_minute_status_uses_log_activity_not_stale_job_dir_mtime(tmp_path: Path) -> None:
    from quantdesk.tushare_minute_status import build_tushare_minute_status

    job = tmp_path / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_project_real_2656_20260705_184207"
    job.mkdir(parents=True)
    (job / "preflight.log").write_text("[2026-07-05T18:42:10] starting python downloader\n", encoding="utf-8")
    (job / "stdout.log").write_text(
        "[2026-07-05T18:45:43] SYMBOL_DONE 1/2656 symbol=688403 rows=60248 status=ok after_rows=60248\n",
        encoding="utf-8",
    )
    (job / "summary.json").write_text(
        json.dumps(
            {
                "status": "running",
                "symbol_count": 2656,
                "target_end": "20260703",
                "download": [{"symbol": "688403", "status": "ok", "rows": 60248}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    old_timestamp = time.time() - 3600
    os.utime(job, (old_timestamp, old_timestamp))

    payload = build_tushare_minute_status(
        project_root=tmp_path,
        include_processes=False,
        stale_after_seconds=900,
    )

    assert payload["status"] == "running"
    assert payload["suggested_next_action"] == "continue_monitoring"
    assert payload["job"]["age_seconds"] < 900


def test_tushare_minute_status_keeps_running_job_with_old_traceback_as_warning(tmp_path: Path) -> None:
    from quantdesk.tushare_minute_status import build_tushare_minute_status

    job = tmp_path / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_project_real_2656_20260705_184207"
    job.mkdir(parents=True)
    (job / "preflight.log").write_text("[2026-07-05T18:42:10] starting python downloader\n", encoding="utf-8")
    (job / "stdout.log").write_text(
        "\n".join(
            [
                "Traceback (most recent call last):",
                "transient provider warning after one request",
                "[2026-07-05T18:45:43] SYMBOL_DONE 1/2656 symbol=688403 rows=60248 status=partial after_rows=60248",
                "[2026-07-05T18:46:43] OK symbol=600711 chunk=20250624..20250721 rows=4820",
                "[2026-07-05T18:47:43] SYMBOL_DONE 2/2656 symbol=600711 rows=60247 status=ok after_rows=60247",
            ]
        ),
        encoding="utf-8",
    )
    (job / "summary.json").write_text(
        json.dumps(
            {
                "status": "running",
                "symbol_count": 2656,
                "target_end": "20260703",
                "download": [
                    {"symbol": "688403", "status": "partial", "rows": 60248},
                    {"symbol": "600711", "status": "ok", "rows": 60247},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = build_tushare_minute_status(
        project_root=tmp_path,
        include_processes=False,
        stale_after_seconds=900,
    )

    assert payload["status"] == "running_with_warnings"
    assert payload["suggested_next_action"] == "continue_monitoring"
    assert payload["log_summary"]["traceback_count"] == 1
    assert payload["log_summary"]["symbol_done_count"] == 2
    assert payload["log_summary"]["last_symbol_done"]["symbol"] == "600711"
    assert "traceback_count:1" in payload["warnings"]


def test_tushare_minute_status_surfaces_completed_quality_pool(tmp_path: Path) -> None:
    from quantdesk.tushare_minute_status import build_tushare_minute_status, render_tushare_minute_status_report

    job = tmp_path / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_project_real_2656_20260705_184207"
    job.mkdir(parents=True)
    (job / "preflight.log").write_text("[2026-07-05T18:42:10] starting python downloader\n", encoding="utf-8")
    (job / "stdout.log").write_text(
        "[2026-07-05T18:45:43] SYMBOL_DONE 1/2656 symbol=688403 rows=60248 status=ok after_rows=60248\n",
        encoding="utf-8",
    )
    (job / "summary.json").write_text(
        json.dumps({"status": "running", "symbol_count": 2656, "target_end": "20260703", "download": []}),
        encoding="utf-8",
    )
    snapshot_path = tmp_path / "results" / "data_health" / "tushare_minute_quality_latest.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "task_type": "tushare_minute_quality_audit",
                "status": "passed",
                "job": {
                    "path": "data/vendor_staging/tushare_proxy_minutes_250d_phase2_top200_20260704_163110",
                    "summary_symbol_count": 200,
                    "target_end": "2026-07-03",
                },
                "target_date": "2026-07-03",
                "total_symbols": 200,
                "quality_summary": {"usable": 200, "partial": 0, "insufficient": 0, "invalid": 0, "missing": 0},
                "suggested_next_action": "use_completed_minute_pool_for_research",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = build_tushare_minute_status(project_root=tmp_path, include_processes=False)

    quality = payload["latest_completed_quality"]
    assert quality["status"] == "passed"
    assert quality["usable_count"] == 200
    assert quality["total_symbols"] == 200
    assert quality["suggested_next_action"] == "use_completed_minute_pool_for_research"
    report = render_tushare_minute_status_report(payload)
    assert "latest_completed_quality" in report
    assert "use_completed_minute_pool_for_research" in report


def test_tushare_minute_status_process_check_ignores_own_powershell_query(monkeypatch, tmp_path: Path) -> None:
    from quantdesk import tushare_minute_status

    job = tmp_path / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_project_real_2656_20260705_184207"
    job.mkdir(parents=True)

    def fake_run(*args, **kwargs):  # noqa: ANN001, ANN202 - local subprocess stub
        command = args[0][-1]
        assert "$_.ProcessId -ne $PID" in command
        assert "-and ($_.CommandLine" in command
        assert f"*{job.name}*') }}" in command
        rows = [
            {
                "ProcessId": 111,
                "ParentProcessId": 1,
                "Name": "powershell.exe",
                "CommandLine": (
                    "powershell.exe -Command Get-CimInstance Win32_Process | "
                    "Where-Object { $_.CommandLine -like '*tushare_minute_probe_and_download.py*' }"
                ),
            },
            {
                "ProcessId": 222,
                "ParentProcessId": 1,
                "Name": "python.exe",
                "CommandLine": (
                    "python .tmp\\tushare_minute_probe_and_download.py "
                    f"--out-dir {job}"
                ),
            },
        ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(tushare_minute_status.subprocess, "run", fake_run)

    rows = tushare_minute_status._process_matches(job)

    assert [row["pid"] for row in rows] == [222]


def test_tushare_minute_status_cli_and_qd_help_contract(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            PYTHON,
            str(PROJECT_ROOT / "scripts" / "show_tushare_minute_status.py"),
            "--project-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "minute_status.json"),
            "--report",
            str(tmp_path / "minute_status.md"),
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
    assert payload["task_type"] == "tushare_minute_status"
    assert (tmp_path / "minute_status.json").exists()
    assert (tmp_path / "minute_status.md").exists()

    help_text = subprocess.check_output(
        [PYTHON, str(PROJECT_ROOT / "scripts" / "qd.py"), "help"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert "tushare-minute-status" in help_text
