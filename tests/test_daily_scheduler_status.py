from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _linux_scheduler_payload(
    project_root: Path,
    *,
    last_result: object = 0,
    **overrides: object,
) -> dict:
    from quantdesk.daily_scheduler_status import (
        LINUX_STATUS_SCHEMA,
        LINUX_TASK_ID,
    )

    payload = {
        "schema": LINUX_STATUS_SCHEMA,
        "task": LINUX_TASK_ID,
        "runner": {
            "project_root": str(project_root.resolve()),
            "enabled": True,
            "state": "ready",
        },
        "last_run": {
            "time": "2026-08-16T15:45:00+08:00",
            "result": last_result,
        },
        "next_run": {"time": "2026-08-17T15:45:00+08:00"},
    }
    payload.update(overrides)
    return payload


def _write_linux_status(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _runner(payload: dict, *, returncode: int = 0, stderr: str = ""):
    def run(command, **kwargs):
        assert "QuantDesk Daily Data Chain" in " ".join(command)
        return SimpleNamespace(
            returncode=returncode,
            stdout=json.dumps(payload),
            stderr=stderr,
        )

    return run


def _managed_task_payload(
    project_root: Path,
    *,
    last_task_result: object = 0,
    last_task_result_present: bool = True,
    **overrides: object,
) -> dict:
    runner = project_root / "scripts" / "run_daily_data_chain.cmd"
    payload = {
        "exists": True,
        "managed": True,
        "enabled": True,
        "state": "Ready",
        "last_run_time": "2026-07-17T15:45:00",
        "last_task_result_present": last_task_result_present,
        "last_task_result": last_task_result,
        "next_run_time": "2026-07-17T18:30:00",
        "execute": "C:\\Windows\\System32\\cmd.exe",
        "arguments": f'/d /s /c ""{runner}" --enable-network"',
        "working_directory": str(project_root),
    }
    payload.update(overrides)
    return payload


def test_linux_scheduler_status_reads_valid_audited_status_file(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    status_path = tmp_path / "runtime" / "scheduler.json"
    _write_linux_status(status_path, _linux_scheduler_payload(tmp_path))

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="linux",
        status_path=status_path,
    )

    assert payload["ok"] is True
    assert payload["status"] == "healthy"
    assert payload["automatic_update_configured"] is True
    assert payload["last_run_succeeded"] is True
    assert payload["last_task_result"] == 0
    assert payload["last_run_time"] == "2026-08-16T15:45:00+08:00"
    assert payload["next_run_time"] == "2026-08-17T15:45:00+08:00"
    assert payload["runner_project_root"] == str(tmp_path.resolve())
    assert payload["status_path"] == str(status_path.resolve())


def test_linux_scheduler_status_uses_project_state_default_path(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    status_path = (
        tmp_path / "state" / "scheduler" / "daily_data_chain_status.json"
    )
    _write_linux_status(status_path, _linux_scheduler_payload(tmp_path))

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="linux",
    )

    assert payload["status"] == "healthy"
    assert payload["status_path"] == str(status_path.resolve())


@pytest.mark.parametrize(
    ("file_value", "expected_reason"),
    [
        (None, "linux_scheduler_status_missing"),
        ("{not-json", "linux_scheduler_status_invalid_json"),
    ],
)
def test_linux_scheduler_status_fails_closed_for_missing_or_bad_json(
    tmp_path: Path,
    file_value: str | None,
    expected_reason: str,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    status_path = tmp_path / "scheduler.json"
    if file_value is not None:
        status_path.write_text(file_value, encoding="utf-8")

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="linux",
        status_path=status_path,
    )

    assert payload["ok"] is False
    assert payload["status"] == "unavailable"
    assert payload["automatic_update_configured"] is False
    assert payload["last_run_succeeded"] is False
    assert payload["reason_codes"] == [expected_reason]


@pytest.mark.parametrize(
    ("mutator", "expected_reason"),
    [
        (
            lambda payload, root: payload.__setitem__("schema", "wrong"),
            "linux_scheduler_schema_mismatch",
        ),
        (
            lambda payload, root: payload.__setitem__("task", "wrong"),
            "linux_scheduler_task_mismatch",
        ),
        (
            lambda payload, root: payload["runner"].__setitem__(
                "project_root", str(root / "stale_checkout")
            ),
            "linux_scheduler_project_root_mismatch",
        ),
        (
            lambda payload, root: payload["last_run"].__setitem__(
                "time", "not-a-time"
            ),
            "linux_scheduler_last_run_invalid",
        ),
        (
            lambda payload, root: payload["next_run"].__setitem__(
                "time", ""
            ),
            "linux_scheduler_next_run_invalid",
        ),
    ],
)
def test_linux_scheduler_status_fails_closed_for_contract_drift(
    tmp_path: Path,
    mutator,
    expected_reason: str,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    raw = _linux_scheduler_payload(tmp_path)
    mutator(raw, tmp_path)
    status_path = tmp_path / "scheduler.json"
    _write_linux_status(status_path, raw)

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="linux",
        status_path=status_path,
    )

    assert payload["ok"] is False
    assert payload["status"] == "misconfigured"
    assert payload["automatic_update_configured"] is False
    assert payload["last_run_succeeded"] is False
    assert expected_reason in payload["reason_codes"]


def test_linux_scheduler_status_reports_failed_last_run(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    status_path = tmp_path / "scheduler.json"
    _write_linux_status(
        status_path,
        _linux_scheduler_payload(tmp_path, last_result=4),
    )

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="linux",
        status_path=status_path,
    )

    assert payload["ok"] is True
    assert payload["status"] == "last_run_failed"
    assert payload["automatic_update_configured"] is True
    assert payload["last_run_succeeded"] is False
    assert payload["last_task_result"] == 4
    assert payload["reason_codes"] == ["scheduled_chain_last_run_failed"]


def test_scheduler_status_marks_enabled_managed_task_as_first_run_pending(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner(
            _managed_task_payload(
                tmp_path,
                last_task_result=267011,
                last_run_time="1999-11-30T00:00:00",
            )
        ),
    )

    assert payload["status"] == "ready_first_run_pending"
    assert payload["automatic_update_configured"] is True
    assert payload["first_run_pending"] is True
    assert payload["last_run_succeeded"] is False
    assert payload["last_task_result_hex"] == "0x00041303"
    assert payload["boundaries"]["network_requires_explicit_flag"] is True


def test_scheduler_status_exposes_failed_last_run_without_claiming_healthy(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner(
            _managed_task_payload(tmp_path, last_task_result=4)
        ),
    )

    assert payload["status"] == "last_run_failed"
    assert payload["automatic_update_configured"] is True
    assert payload["last_run_succeeded"] is False
    assert payload["last_task_result"] == 4
    assert payload["reason_codes"] == ["scheduled_chain_last_run_failed"]


@pytest.mark.parametrize(
    ("last_task_result", "present", "expected_reason"),
    [
        (None, False, "scheduled_task_last_result_missing"),
        ("not-an-integer", True, "scheduled_task_last_result_invalid"),
        (True, True, "scheduled_task_last_result_invalid"),
    ],
)
def test_scheduler_status_fails_closed_when_last_result_is_not_valid(
    tmp_path: Path,
    last_task_result: object,
    present: bool,
    expected_reason: str,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner(
            _managed_task_payload(
                tmp_path,
                last_task_result=last_task_result,
                last_task_result_present=present,
            )
        ),
    )

    assert payload["status"] == "last_run_unknown"
    assert payload["automatic_update_configured"] is True
    assert payload["last_run_succeeded"] is False
    assert payload["last_task_result"] is None
    assert payload["last_task_result_hex"] == ""
    assert expected_reason in payload["reason_codes"]


def test_scheduler_status_rejects_action_from_another_project_root(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    stale_root = tmp_path.parent / "stale_checkout"
    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner(_managed_task_payload(stale_root)),
    )

    assert payload["status"] == "misconfigured"
    assert payload["automatic_update_configured"] is False
    assert payload["last_run_succeeded"] is False
    assert payload["action_contract_matches"] is False
    assert "scheduled_task_action_mismatch" in payload["reason_codes"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"execute": "C:\\Temp\\cmd.exe"},
        {
            "arguments": (
                '\\d \\s \\c ""{runner}" --enable-network"'
            )
        },
        {"working_directory": "."},
    ],
)
def test_scheduler_status_rejects_near_match_action_contract(
    tmp_path: Path,
    overrides: dict[str, str],
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    runner = tmp_path / "scripts" / "run_daily_data_chain.cmd"
    resolved = {
        key: value.format(runner=runner) for key, value in overrides.items()
    }
    raw = _managed_task_payload(tmp_path, **resolved)
    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner(raw),
    )

    assert payload["status"] == "misconfigured"
    assert payload["action_contract_matches"] is False
    assert payload["automatic_update_configured"] is False


def test_scheduler_status_accepts_exact_action_for_current_project_root(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    raw = _managed_task_payload(tmp_path)
    raw["execute"] = str(raw["execute"]).upper()
    raw["working_directory"] = str(raw["working_directory"]).upper()
    raw["arguments"] = str(raw["arguments"]).upper()
    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner(raw),
    )

    assert payload["status"] == "healthy"
    assert payload["automatic_update_configured"] is True
    assert payload["last_run_succeeded"] is True
    assert payload["action_contract_matches"] is True


def test_scheduler_status_accepts_current_powershell_supervisor_action(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_scheduler_status import (
        _expected_powershell_executable,
        daily_data_scheduler_status,
    )

    runner = tmp_path / "scripts" / "run_daily_data_chain_task.ps1"
    raw = _managed_task_payload(tmp_path)
    raw.update(
        {
            "execute": _expected_powershell_executable(),
            "arguments": (
                '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File '
                f'"{runner}" -EnableNetwork'
            ),
        }
    )

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner(raw),
    )

    assert payload["status"] == "healthy"
    assert payload["automatic_update_configured"] is True
    assert payload["action_contract_matches"] is True
    assert payload["action_contract_kind"] == "powershell_supervisor"


def test_scheduler_probe_preserves_missing_result_and_working_directory() -> None:
    from quantdesk.daily_scheduler_status import _probe_command

    script = _probe_command("QuantDesk Daily Data Chain")[-1]

    assert "last_task_result_present" in script
    assert "working_directory" in script
    assert "last_task_result = [int64]$info.LastTaskResult" not in script


def test_scheduler_status_fails_closed_when_probe_is_unavailable(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner({}, returncode=1, stderr="access denied"),
    )

    assert payload["status"] == "unavailable"
    assert payload["automatic_update_configured"] is False
    assert payload["reason_codes"] == ["scheduled_task_probe_failed"]
    assert "access denied" in payload["error"]


def test_data_health_payload_exposes_scheduler_status_without_reprobing() -> None:
    from quantdesk.data_health_dashboard import build_data_health_dashboard

    scheduler = {
        "status": "healthy",
        "automatic_update_configured": True,
        "last_run_succeeded": True,
        "last_task_result": 0,
        "next_run_time": "2026-07-17T18:30:00+08:00",
    }
    payload = build_data_health_dashboard(
        daily_price_payload={
            "target_date": "2026-07-17",
            "latest_local_date": "2026-07-17",
            "total_symbols": 1,
            "stale_count": 0,
            "missing_count": 0,
        },
        freshness_payload={
            "target_complete_trade_date": "2026-07-17",
            "local_daily_latest_date": "2026-07-17",
            "daily_feature_date": "2026-07-17",
            "is_daily_stale": False,
            "portfolio_review_valid": True,
        },
        intraday_payload={"target_trade_date": "2026-07-17", "total_symbols": 0},
        theme_payload={"exists": True, "total": 0, "mapped_count": 0, "unknown_count": 0},
        current_candidate_theme_payload={"candidate_count": 0},
        provider_capability_payload={"summary": {"ok_count": 0}},
        portfolio_payload={"exists": True, "positions": []},
        daily_workflow_payload={},
        real_sample_payload={},
        self_validation_payload={},
        strategy_payload={"long_candidates": []},
        extended_gates_payload={},
        daily_data_chain_payload={"status": "not_run"},
        daily_scheduler_payload=scheduler,
        theme_map={},
    )

    assert payload["daily_data_scheduler"] is scheduler
    assert payload["source_status"]["daily_data_scheduler"] is scheduler


def test_data_health_cli_snapshot_persists_daily_chain_scheduler_overlay() -> None:
    from scripts.run_data_health_dashboard import build_daily_chain_snapshot_overlay

    chain = {
        "status": "partial",
        "authoritative": True,
        "run_id": "daily-data-chain-test",
        "target_date": "2026-07-17",
        "daily_data_chain_contract_version": 9,
        "retry_symbols": ["920685"],
        "universe": {
            "current_analysis_pool": {
                "symbol_count": 914,
                "sha256": "pool-sha",
            }
        },
        "coverage": {
            "daily": {"status": "passed", "coverage_ratio": 1.0},
            "critical_daily": {"status": "passed", "coverage_ratio": 1.0},
            "critical": {"status": "passed", "coverage_ratio": 1.0},
            "current_analysis_pool": {
                "status": "partial",
                "requested_count": 914,
                "complete_session_count": 913,
                "coverage_ratio": 0.998906,
                "failed_symbols": ["920685"],
            },
        },
        "components": {
            "conditional_plan_freeze": {
                "status": "blocked",
                "written": False,
                "reason_codes": ["upstream_data_chain_not_completed"],
            }
        },
    }
    scheduler = {
        "status": "ready_first_run_pending",
        "automatic_update_configured": True,
        "first_run_pending": True,
        "last_task_result": 267011,
        "next_run_time": "2026-07-20T15:45:00+08:00",
    }

    payload = build_daily_chain_snapshot_overlay(chain, scheduler)

    assert payload["daily_data_scheduler"] is scheduler
    assert payload["formal_daily_chain_status"]["scheduler"]["enabled"] is True
    assert payload["formal_daily_chain_status"]["missing_or_retry_symbols"] == ["920685"]
    assert payload["downstream_consumer_gate"]["allowed"] is False
    assert "daily_chain_status_partial" in payload["downstream_consumer_gate"]["reasons"]


def test_scheduler_status_accepts_legacy_cmd_action(tmp_path: Path) -> None:
    """The pre-existing cmd.exe task must stay valid until re-registration."""

    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    raw = _managed_task_payload(tmp_path, trigger_count=2, enabled_trigger_count=2)

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner(raw),
    )

    assert payload["status"] == "healthy"
    assert payload["action_contract_kind"] == "cmd_compat"
    assert payload["action_contract_matches"] is True
    assert payload["automatic_update_configured"] is True


def test_scheduler_status_accepts_powershell_action_with_triggers(tmp_path: Path) -> None:
    from quantdesk.daily_scheduler_status import (
        _expected_powershell_executable,
        daily_data_scheduler_status,
    )

    runner = tmp_path / "scripts" / "run_daily_data_chain_task.ps1"
    raw = _managed_task_payload(tmp_path, trigger_count=1, enabled_trigger_count=1)
    raw.update(
        {
            "execute": _expected_powershell_executable(),
            "arguments": (
                '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File '
                f'"{runner}" -EnableNetwork'
            ),
        }
    )

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner(raw),
    )

    assert payload["status"] == "healthy"
    assert payload["action_contract_kind"] == "powershell_supervisor"
    assert payload["has_enabled_trigger"] is True


def test_scheduler_status_is_misconfigured_when_no_enabled_trigger(tmp_path: Path) -> None:
    """A valid action with no enabled trigger never fires; it is not healthy."""

    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    raw = _managed_task_payload(tmp_path, trigger_count=1, enabled_trigger_count=0)

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner(raw),
    )

    assert payload["status"] == "misconfigured"
    assert "scheduled_task_trigger_missing" in payload["reason_codes"]
    assert payload["automatic_update_configured"] is False
    assert payload["has_enabled_trigger"] is False


def test_scheduler_status_tolerates_probe_payload_without_trigger_counts(tmp_path: Path) -> None:
    """Older probe payloads omit trigger counts; that must stay non-blocking."""

    from quantdesk.daily_scheduler_status import daily_data_scheduler_status

    raw = _managed_task_payload(tmp_path)
    assert "trigger_count" not in raw

    payload = daily_data_scheduler_status(
        project_root=tmp_path,
        platform_name="win32",
        runner=_runner(raw),
    )

    assert payload["status"] == "healthy"
    assert payload["has_enabled_trigger"] is None
    assert payload["automatic_update_configured"] is True


def test_scheduler_probe_collects_trigger_state() -> None:
    from quantdesk.daily_scheduler_status import _probe_command

    script = _probe_command("QuantDesk Daily Data Chain")[-1]

    assert "$task.Triggers" in script
    assert "trigger_count" in script
    assert "enabled_trigger_count" in script


def test_snapshot_status_degrades_to_failed_when_latest_chain_blocked() -> None:
    """A cached snapshot must not persist a green verdict over a blocked chain.

    Regression guard: the persisted `status` came only from the component parts,
    so an all-green parts set wrote `passed` while the embedded latest chain was
    `blocked` -- the dashboard then displayed cached health that contradicted
    the live state.
    """

    from scripts.run_data_health_dashboard import _reconcile_status_with_chain

    status, reasons = _reconcile_status_with_chain(
        "passed",
        daily_chain={"status": "blocked"},
        scheduler={"status": "healthy"},
    )

    assert status == "failed"
    assert "latest_daily_chain_blocked" in reasons


def test_snapshot_status_degrades_to_failed_when_latest_chain_abandoned() -> None:
    from scripts.run_data_health_dashboard import _reconcile_status_with_chain

    status, reasons = _reconcile_status_with_chain(
        "passed",
        daily_chain={"status": "abandoned"},
        scheduler={"status": "healthy"},
    )

    assert status == "failed"
    assert "latest_daily_chain_abandoned" in reasons


def test_operation_log_status_preserves_failed_payload() -> None:
    from scripts.run_data_health_dashboard import _operation_log_status

    assert _operation_log_status("failed") == "failed"
    assert _operation_log_status("warning") == "warning"
    assert _operation_log_status("passed") == "passed"


def test_snapshot_status_degrades_to_warning_when_latest_chain_running() -> None:
    from scripts.run_data_health_dashboard import _reconcile_status_with_chain

    status, reasons = _reconcile_status_with_chain(
        "passed",
        daily_chain={"status": "running"},
        scheduler={"status": "healthy"},
    )

    assert status == "warning"
    assert "latest_daily_chain_running" in reasons


def test_snapshot_status_degrades_when_scheduler_misconfigured() -> None:
    from scripts.run_data_health_dashboard import _reconcile_status_with_chain

    status, reasons = _reconcile_status_with_chain(
        "passed",
        daily_chain={"status": "completed"},
        scheduler={"status": "misconfigured"},
    )

    assert status == "failed"
    assert "daily_data_scheduler_misconfigured" in reasons


def test_snapshot_status_preserves_clean_verdict_and_never_upgrades() -> None:
    from scripts.run_data_health_dashboard import _reconcile_status_with_chain

    clean, reasons = _reconcile_status_with_chain(
        "passed",
        daily_chain={"status": "completed"},
        scheduler={"status": "healthy"},
    )
    assert clean == "passed"
    assert reasons == []

    # An existing component failure is never softened by a healthy chain.
    kept, _ = _reconcile_status_with_chain(
        "failed",
        daily_chain={"status": "completed"},
        scheduler={"status": "healthy"},
    )
    assert kept == "failed"

    # A component warning stays a warning rather than being promoted.
    warned, _ = _reconcile_status_with_chain(
        "warning",
        daily_chain={"status": "completed"},
        scheduler={"status": "healthy"},
    )
    assert warned == "warning"
