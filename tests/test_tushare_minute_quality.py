from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _write_minute_csv(path: Path, *, symbol: str, date: str = "2026-07-03", rows: int = 181) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["symbol,datetime,date,time,open,high,low,close,volume,amount,source,updated_at"]
    for index in range(rows):
        minute = 31 + index
        hour = 9 + minute // 60
        minute_in_hour = minute % 60
        time_text = f"{hour:02d}:{minute_in_hour:02d}:00"
        price = 10 + index * 0.001
        lines.append(
            f"{symbol},{date} {time_text},{date},{time_text},"
            f"{price:.3f},{price + 0.01:.3f},{price - 0.01:.3f},{price:.3f},100,1000,tushare_proxy_stk_mins,2026-07-05T00:00:00"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def test_tushare_minute_quality_audits_latest_completed_job(tmp_path: Path) -> None:
    from quantdesk.tushare_minute_quality import build_tushare_minute_quality_audit, render_tushare_minute_quality_report

    job = tmp_path / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_phase2_top200_20260704_163110"
    job.mkdir(parents=True)
    (job / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "symbol_count": 2,
                "target_end": "20260703",
                "days": 250,
                "download": [
                    {"symbol": "600000", "status": "ok", "rows": 60000, "raw_path": "must_not_leak"},
                    {"symbol": "000001", "status": "partial", "rows": 100, "errors": ["timeout"]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_minute_csv(tmp_path / "data" / "intraday" / "1m" / "600000.csv", symbol="600000")

    payload = build_tushare_minute_quality_audit(project_root=tmp_path, max_items=10)

    assert payload["task_type"] == "tushare_minute_quality_audit"
    assert payload["status"] == "warning"
    assert payload["job"]["path"].endswith("tushare_proxy_minutes_250d_phase2_top200_20260704_163110")
    assert payload["target_date"] == "2026-07-03"
    assert payload["total_symbols"] == 2
    assert payload["quality_summary"]["usable"] == 1
    assert payload["quality_summary"]["missing"] == 1
    assert payload["download_status_counts"] == {"ok": 1, "partial": 1}
    assert payload["read_only"] is True
    assert payload["canonical_write"] is False
    assert payload["network_used"] is False
    assert "must_not_leak" not in json.dumps(payload, ensure_ascii=False)
    report = render_tushare_minute_quality_report(payload)
    assert "Tushare Minute Quality Audit" in report
    assert "usable" in report


def test_tushare_minute_quality_cli_and_qd_help_contract(tmp_path: Path) -> None:
    job = tmp_path / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_phase2_top200_20260704_163110"
    job.mkdir(parents=True)
    (job / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "symbol_count": 1,
                "target_end": "20260703",
                "download": [{"symbol": "600000", "status": "ok", "rows": 60000}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_minute_csv(tmp_path / "data" / "intraday" / "1m" / "600000.csv", symbol="600000")

    completed = subprocess.run(
        [
            PYTHON,
            str(PROJECT_ROOT / "scripts" / "show_tushare_minute_quality.py"),
            "--project-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "minute_quality.json"),
            "--report",
            str(tmp_path / "minute_quality.md"),
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
    assert payload["quality_summary"]["usable"] == 1
    assert (tmp_path / "minute_quality.json").exists()
    assert (tmp_path / "minute_quality.md").exists()

    help_text = subprocess.check_output(
        [PYTHON, str(PROJECT_ROOT / "scripts" / "qd.py"), "help"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert "tushare-minute-quality" in help_text


def test_tushare_minute_quality_snapshot_summary_is_cheap_and_match_checked(tmp_path: Path) -> None:
    from quantdesk.tushare_minute_quality import (
        load_latest_tushare_minute_quality_snapshot,
        summarize_completed_minute_quality_snapshot,
    )

    snapshot = tmp_path / "results" / "data_health" / "tushare_minute_quality_latest.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        json.dumps(
            {
                "task_type": "tushare_minute_quality_audit",
                "status": "passed",
                "job": {"path": "data/vendor_staging/tushare_proxy_minutes_250d_phase2_top200_x"},
                "target_date": "2026-07-03",
                "total_symbols": 200,
                "quality_summary": {
                    "usable": 200,
                    "partial": 0,
                    "insufficient": 0,
                    "invalid": 0,
                    "missing": 0,
                },
                "suggested_next_action": "use_completed_minute_pool_for_research",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = load_latest_tushare_minute_quality_snapshot(project_root=tmp_path)
    summary = summarize_completed_minute_quality_snapshot(
        payload,
        expected_job_path="data/vendor_staging/tushare_proxy_minutes_250d_phase2_top200_x",
    )

    assert payload["snapshot_available"] is True
    assert summary["status"] == "passed"
    assert summary["job_matches_latest_completed"] is True
    assert summary["total_symbols"] == 200
    assert summary["usable_count"] == 200
    assert summary["invalid_count"] == 0
    assert summary["missing_count"] == 0
    assert summary["suggested_next_action"] == "use_completed_minute_pool_for_research"

    stale = summarize_completed_minute_quality_snapshot(
        payload,
        expected_job_path="data/vendor_staging/tushare_proxy_minutes_250d_newer_job",
    )
    assert stale["status"] == "stale_or_mismatch"
    assert stale["job_matches_latest_completed"] is False


def _write_completed_job_summary(root: Path, job_name: str, download: list[dict]) -> Path:
    job = root / "data" / "vendor_staging" / job_name
    job.mkdir(parents=True, exist_ok=True)
    (job / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "symbol_count": len(download),
                "target_end": "20260703",
                "days": 250,
                "download": download,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return job


def _quality_snapshot_payload(job_name: str, *, status: str, usable: int, partial: int, total: int) -> dict:
    return {
        "task_type": "tushare_minute_quality_audit",
        "status": status,
        "job": {"path": f"data/vendor_staging/{job_name}"},
        "target_date": "2026-07-03",
        "total_symbols": total,
        "quality_summary": {
            "usable": usable,
            "partial": partial,
            "insufficient": 0,
            "invalid": 0,
            "missing": 0,
        },
        "suggested_next_action": "use_completed_minute_pool_for_research" if status == "passed" else "review_minute_quality_warnings",
    }


def test_verified_pool_falls_back_to_last_passed_snapshot_when_latest_not_passed(tmp_path: Path) -> None:
    from quantdesk.tushare_minute_quality import build_verified_minute_pool_snapshot

    top200_job = "tushare_proxy_minutes_250d_phase2_top200_20260704_163110"
    big_job = "tushare_proxy_minutes_250d_project_real_2656_20260707_090000"
    _write_completed_job_summary(tmp_path, top200_job, [{"symbol": "600000", "status": "ok", "rows": 60000}])
    _write_completed_job_summary(
        tmp_path,
        big_job,
        [
            {"symbol": "600000", "status": "ok", "rows": 60000},
            {"symbol": "000001", "status": "partial", "rows": 100},
        ],
    )
    snapshots = tmp_path / "results" / "data_health"
    snapshots.mkdir(parents=True)
    (snapshots / "tushare_minute_quality_last_passed.json").write_text(
        json.dumps(_quality_snapshot_payload(top200_job, status="passed", usable=1, partial=0, total=1), ensure_ascii=False),
        encoding="utf-8",
    )
    (snapshots / "tushare_minute_quality_latest.json").write_text(
        json.dumps(_quality_snapshot_payload(big_job, status="partial", usable=1, partial=1, total=2), ensure_ascii=False),
        encoding="utf-8",
    )

    pool = build_verified_minute_pool_snapshot(project_root=tmp_path)

    assert pool["status"] == "passed"
    assert pool["source_snapshot"] == "last_passed"
    assert pool["symbols"] == ["600000"]
    assert pool["job_path"].endswith(top200_job)
    assert pool["latest_quality_status"] == "partial"
    assert "verified_pool_from_last_passed_snapshot" in pool["warnings"]
    assert "latest_quality_snapshot_status:partial" in pool["warnings"]


def test_verified_pool_stays_empty_when_latest_not_passed_and_no_last_passed(tmp_path: Path) -> None:
    from quantdesk.tushare_minute_quality import build_verified_minute_pool_snapshot

    big_job = "tushare_proxy_minutes_250d_project_real_2656_20260707_090000"
    _write_completed_job_summary(
        tmp_path,
        big_job,
        [
            {"symbol": "600000", "status": "ok", "rows": 60000},
            {"symbol": "000001", "status": "partial", "rows": 100},
        ],
    )
    snapshots = tmp_path / "results" / "data_health"
    snapshots.mkdir(parents=True)
    (snapshots / "tushare_minute_quality_latest.json").write_text(
        json.dumps(_quality_snapshot_payload(big_job, status="partial", usable=1, partial=1, total=2), ensure_ascii=False),
        encoding="utf-8",
    )

    pool = build_verified_minute_pool_snapshot(project_root=tmp_path)

    assert pool["status"] == "warning"
    assert pool["source_snapshot"] == "latest"
    assert pool["symbols"] == []


def test_write_quality_outputs_preserves_last_passed_snapshot_across_newer_partial_job(tmp_path: Path) -> None:
    import os

    from quantdesk.tushare_minute_quality import (
        build_verified_minute_pool_snapshot,
        write_tushare_minute_quality_outputs,
    )

    output_path = tmp_path / "results" / "data_health" / "tushare_minute_quality_latest.json"
    report_path = tmp_path / "reports" / "tushare_minute_quality_latest.md"
    last_passed_path = output_path.with_name("tushare_minute_quality_last_passed.json")
    _write_minute_csv(tmp_path / "data" / "intraday" / "1m" / "600000.csv", symbol="600000")

    top200_job = _write_completed_job_summary(
        tmp_path,
        "tushare_proxy_minutes_250d_phase2_top200_20260704_163110",
        [{"symbol": "600000", "status": "ok", "rows": 60000}],
    )
    first = write_tushare_minute_quality_outputs(
        project_root=tmp_path,
        output_path=output_path,
        report_path=report_path,
    )
    assert first["status"] == "passed"
    assert last_passed_path.exists()

    big_job = _write_completed_job_summary(
        tmp_path,
        "tushare_proxy_minutes_250d_project_real_2656_20260707_090000",
        [
            {"symbol": "600000", "status": "ok", "rows": 60000},
            {"symbol": "000001", "status": "partial", "rows": 100},
        ],
    )
    old_time = top200_job.stat().st_mtime - 3600
    os.utime(top200_job, (old_time, old_time))
    newer_time = top200_job.stat().st_mtime + 7200
    os.utime(big_job, (newer_time, newer_time))

    second = write_tushare_minute_quality_outputs(
        project_root=tmp_path,
        output_path=output_path,
        report_path=report_path,
    )
    assert second["status"] == "warning"
    assert second["job"]["path"].endswith("tushare_proxy_minutes_250d_project_real_2656_20260707_090000")

    preserved = json.loads(last_passed_path.read_text(encoding="utf-8"))
    assert preserved["status"] == "passed"
    assert preserved["job"]["path"].endswith("tushare_proxy_minutes_250d_phase2_top200_20260704_163110")

    pool = build_verified_minute_pool_snapshot(project_root=tmp_path)
    assert pool["status"] == "passed"
    assert pool["source_snapshot"] == "last_passed"
    assert pool["symbols"] == ["600000"]
