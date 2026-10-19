from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

from quantdesk.interprocess_lock import interprocess_file_lock
from scripts.run_scheduled_task import run_task, task_log_path


def _action(root: Path) -> dict[str, str]:
    return {"executable": "never-execute", "arguments": "--token secret-fixture", "working_directory": str(root)}


def test_start_and_finish_share_run_id_and_no_arguments_are_logged(tmp_path: Path) -> None:
    runner = Mock(return_value=subprocess.CompletedProcess([], 0))
    assert run_task(tmp_path, "fixture", "daily", _action(tmp_path), runner=runner) == 0
    log = task_log_path(tmp_path, "fixture")
    text = log.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    assert [row["status"] for row in rows] == ["started", "completed"]
    assert rows[0]["run_id"] == rows[1]["run_id"]
    assert rows[0]["code_fingerprint"]["launcher_sha256"]
    assert "secret-fixture" not in text
    assert json.loads(log.with_suffix(".latest.json").read_text())["status"] == "completed"


def test_unfinished_previous_run_is_reported_once_and_never_replayed(tmp_path: Path) -> None:
    log = task_log_path(tmp_path, "fixture")
    log.parent.mkdir(parents=True)
    log.with_suffix(".latest.json").write_text(json.dumps({"status": "started", "run_id": "previous", "at": "2026-10-12T20:00:00+08:00"}), encoding="utf-8")
    runner = Mock(return_value=subprocess.CompletedProcess([], 0))
    for _ in range(2):
        assert run_task(tmp_path, "fixture", "daily", _action(tmp_path), runner=runner) == 0
    records = [json.loads(row) for row in log.read_text().splitlines()]
    interrupted = [row for row in records if row["status"] == "previous_run_interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0]["previous_run_id"] == "previous"
    assert runner.call_count == 2  # only the two explicitly requested current actions


def test_protection_failure_prevents_business_and_has_failure_receipt(tmp_path: Path) -> None:
    runner = Mock()
    protector = Mock(side_effect=OSError("fixture Job failure"))
    assert run_task(tmp_path, "fixture", "daily", _action(tmp_path), runner=runner, protector=protector) == 2
    runner.assert_not_called()
    record = json.loads(task_log_path(tmp_path, "fixture").with_suffix(".latest.json").read_text())
    assert record["status"] == "launcher_failed"


def test_keyboard_interrupt_has_durable_non_success_receipt(tmp_path: Path) -> None:
    runner = Mock(side_effect=KeyboardInterrupt())
    assert run_task(tmp_path, "fixture", "daily", _action(tmp_path), runner=runner) == 130
    record = json.loads(task_log_path(tmp_path, "fixture").with_suffix(".latest.json").read_text())
    assert record["status"] == "interrupted"


def test_concurrent_task_lock_never_overwrites_active_receipt(tmp_path: Path) -> None:
    log = task_log_path(tmp_path, "fixture")
    runner = Mock()
    with interprocess_file_lock(log.with_suffix(".lock"), timeout_seconds=0):
        log.with_suffix(".latest.json").write_text('{"run_id":"active","status":"started"}', encoding="utf-8")
        assert run_task(tmp_path, "fixture", "daily", _action(tmp_path), runner=runner) == 3
    runner.assert_not_called()
    assert json.loads(log.with_suffix(".latest.json").read_text())["run_id"] == "active"
