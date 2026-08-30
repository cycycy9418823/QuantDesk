"""Read-only status probe for the managed daily-data scheduler."""

from __future__ import annotations

import json
import ntpath
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from quantdesk.shared import now_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_NAME = "QuantDesk Daily Data Chain"
OWNERSHIP_MARKER = "QuantDesk managed daily-data-chain task; schema=1;"
FIRST_RUN_PENDING_RESULT = 0x41303
LINUX_STATUS_SCHEMA = "quantdesk.daily_data_chain_scheduler_status.v1"
LINUX_TASK_ID = "daily_data_chain"
LINUX_STATUS_RELATIVE_PATH = Path(
    "state/scheduler/daily_data_chain_status.json"
)

Runner = Callable[..., Any]


def _probe_command(task_name: str) -> list[str]:
    escaped_name = task_name.replace("'", "''")
    escaped_marker = OWNERSHIP_MARKER.replace("'", "''")
    script = rf"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName '{escaped_name}' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $task) {{
  [ordered]@{{ exists = $false; task_name = '{escaped_name}' }} | ConvertTo-Json -Compress
  exit 0
}}
$info = Get-ScheduledTaskInfo -TaskName '{escaped_name}' -ErrorAction Stop
$action = $task.Actions | Select-Object -First 1
$triggers = @($task.Triggers)
$enabledTriggers = @($triggers | Where-Object {{ $_.Enabled -ne $false }})
$lastTaskResultPresent = ($null -ne $info.LastTaskResult)
[ordered]@{{
  exists = $true
  task_name = '{escaped_name}'
  managed = ([string]$task.Description).StartsWith('{escaped_marker}')
  enabled = ([string]$task.State -ne 'Disabled')
  state = [string]$task.State
  last_run_time = if ($info.LastRunTime) {{ $info.LastRunTime.ToString('o') }} else {{ '' }}
  last_task_result_present = [bool]$lastTaskResultPresent
  last_task_result = if ($lastTaskResultPresent) {{ [int64]$info.LastTaskResult }} else {{ $null }}
  next_run_time = if ($info.NextRunTime) {{ $info.NextRunTime.ToString('o') }} else {{ '' }}
  execute = [string]$action.Execute
  arguments = [string]$action.Arguments
  working_directory = [string]$action.WorkingDirectory
  trigger_count = [int]$triggers.Count
  enabled_trigger_count = [int]$enabledTriggers.Count
}} | ConvertTo-Json -Compress
""".strip()
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]


def _base_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "task_name": TASK_NAME,
        "generated_at": now_text(),
        "network": False,
        "read_only": True,
        "automatic_update_configured": False,
        "first_run_pending": False,
        "last_run_succeeded": False,
        "reason_codes": [],
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "broker_route": False,
            "live_execution": False,
            "network_requires_explicit_flag": True,
        },
    }


def _optional_int(value: Any) -> int | None:
    """Coerce a probe field to int, or None when absent/not a real integer."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _windows_path_key(value: Any) -> str:
    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    return ntpath.normcase(ntpath.normpath(text))


def _windows_command_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _expected_cmd_executable() -> str:
    return str(
        os.environ.get("ComSpec")
        or ntpath.join(
            os.environ.get("SystemRoot") or r"C:\Windows",
            "System32",
            "cmd.exe",
        )
    )


def _expected_powershell_executable() -> str:
    """Return the Windows PowerShell executable used by the managed installer."""

    ps_home = str(os.environ.get("PSHOME") or "").strip()
    if ps_home:
        return ntpath.join(ps_home, "powershell.exe")
    return ntpath.join(
        os.environ.get("SystemRoot") or r"C:\Windows",
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )


def _action_contract_kind(raw: dict[str, Any], project_root: Path) -> str:
    """Identify the exact managed action contract, or return an empty string."""

    root = Path(project_root).resolve()
    execute = str(raw.get("execute") or "").strip()
    arguments = _windows_command_key(raw.get("arguments"))
    working_directory_matches = (
        _windows_path_key(raw.get("working_directory"))
        == _windows_path_key(root)
    )
    if not working_directory_matches:
        return ""

    # The installer now launches the supervisor directly. Keep the old cmd
    # action valid for already-registered tasks until the next re-registration.
    cmd_runner = root / "scripts" / "run_daily_data_chain.cmd"
    cmd_arguments = f'/d /s /c ""{cmd_runner}" --enable-network"'
    if (
        _windows_path_key(execute) == _windows_path_key(_expected_cmd_executable())
        and arguments == _windows_command_key(cmd_arguments)
    ):
        return "cmd_compat"

    powershell_runner = root / "scripts" / "run_daily_data_chain_task.ps1"
    powershell_arguments = (
        f'-NoProfile -NonInteractive -ExecutionPolicy Bypass -File '
        f'"{powershell_runner}" -EnableNetwork'
    )
    if (
        _windows_path_key(execute)
        == _windows_path_key(_expected_powershell_executable())
        and arguments == _windows_command_key(powershell_arguments)
    ):
        return "powershell_supervisor"
    return ""


def _action_contract_matches(raw: dict[str, Any], project_root: Path) -> bool:
    return bool(_action_contract_kind(raw, project_root))


def _parse_audited_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _linux_schema_matches(raw: dict[str, Any]) -> bool:
    value = raw.get("schema", raw.get("schema_version"))
    return value in {
        LINUX_STATUS_SCHEMA,
        "daily_data_chain_scheduler_status_v1",
        1,
        "1",
    }


def _linux_task_value(raw: dict[str, Any]) -> Any:
    value = raw.get("task", raw.get("task_name"))
    if isinstance(value, dict):
        return value.get("id", value.get("name"))
    return value


def _linux_task_matches(raw: dict[str, Any]) -> bool:
    return _linux_task_value(raw) in {LINUX_TASK_ID, TASK_NAME}


def _linux_run_fields(
    raw: dict[str, Any],
) -> tuple[Any, Any, Any]:
    last = raw.get("last_run")
    if isinstance(last, dict):
        last_time = last.get("time", last.get("run_time"))
        last_result = last.get(
            "result",
            last.get("exit_code", last.get("task_result")),
        )
    else:
        last_time = raw.get("last_run_time")
        last_result = raw.get("last_task_result")

    next_run = raw.get("next_run")
    if isinstance(next_run, dict):
        next_time = next_run.get("time", next_run.get("run_time"))
    else:
        next_time = raw.get("next_run_time")
    return last_time, last_result, next_time


def _linux_scheduler_status(
    *,
    payload: dict[str, Any],
    project_root: Path,
    status_path: Path | None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    path = Path(status_path or (root / LINUX_STATUS_RELATIVE_PATH)).resolve()
    payload["status_path"] = str(path)

    if not path.is_file():
        payload.update(
            {
                "ok": False,
                "status": "unavailable",
                "reason_codes": ["linux_scheduler_status_missing"],
            }
        )
        return payload

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        payload.update(
            {
                "ok": False,
                "status": "unavailable",
                "reason_codes": ["linux_scheduler_status_invalid_json"],
                "error": str(exc)[:1000],
            }
        )
        return payload
    except OSError as exc:
        payload.update(
            {
                "ok": False,
                "status": "unavailable",
                "reason_codes": ["linux_scheduler_status_unreadable"],
                "error": f"{type(exc).__name__}:{exc}"[:1000],
            }
        )
        return payload

    if not isinstance(raw, dict):
        payload.update(
            {
                "ok": False,
                "status": "misconfigured",
                "reason_codes": ["linux_scheduler_status_invalid_payload"],
            }
        )
        return payload

    runner_raw = raw.get("runner")
    runner = runner_raw if isinstance(runner_raw, dict) else {}
    runner_root_text = str(
        runner.get("project_root", raw.get("runner_project_root")) or ""
    ).strip()
    runner_root_matches = bool(
        runner_root_text and Path(runner_root_text).resolve() == root
    )
    last_time_value, raw_last_result, next_time_value = _linux_run_fields(raw)
    last_time = _parse_audited_time(last_time_value)
    next_time = _parse_audited_time(next_time_value)
    last_result_valid = bool(
        isinstance(raw_last_result, int)
        and not isinstance(raw_last_result, bool)
    )

    reason_codes: list[str] = []
    schema_matches = _linux_schema_matches(raw)
    task_matches = _linux_task_matches(raw)
    if not schema_matches:
        reason_codes.append("linux_scheduler_schema_mismatch")
    if not task_matches:
        reason_codes.append("linux_scheduler_task_mismatch")
    if not runner_root_matches:
        reason_codes.append("linux_scheduler_project_root_mismatch")
    if last_time is None or not last_result_valid:
        reason_codes.append("linux_scheduler_last_run_invalid")
    if next_time is None or (
        last_time is not None and next_time <= last_time
    ):
        reason_codes.append("linux_scheduler_next_run_invalid")

    enabled_value = runner.get("enabled", raw.get("enabled", True))
    enabled = enabled_value is True
    state = str(runner.get("state", raw.get("state")) or "")
    last_result = int(raw_last_result) if last_result_valid else None
    contract_matches = not reason_codes
    configured = bool(contract_matches and enabled)

    if reason_codes:
        status = "misconfigured"
    elif not enabled:
        status = "disabled"
        reason_codes.append("scheduled_daily_data_task_disabled")
    elif state.casefold() == "running":
        status = "running"
    elif last_result == 0:
        status = "healthy"
    else:
        status = "last_run_failed"
        reason_codes.append("scheduled_chain_last_run_failed")

    payload.update(
        {
            "ok": status != "misconfigured",
            "status": status,
            "exists": True,
            "managed": bool(schema_matches and task_matches),
            "enabled": enabled,
            "state": state,
            "action_contract_matches": contract_matches,
            "automatic_update_configured": configured,
            "last_run_succeeded": bool(configured and last_result == 0),
            "last_run_time": str(last_time_value or ""),
            "last_task_result": last_result,
            "last_task_result_hex": (
                f"0x{last_result & 0xFFFFFFFF:08X}"
                if last_result is not None
                else ""
            ),
            "next_run_time": str(next_time_value or ""),
            "runner_project_root": runner_root_text,
            "execute": str(runner.get("execute") or ""),
            "arguments": str(runner.get("arguments") or ""),
            "working_directory": runner_root_text,
            "reason_codes": reason_codes,
        }
    )
    return payload


def daily_data_scheduler_status(
    *,
    project_root: Path = PROJECT_ROOT,
    platform_name: str | None = None,
    runner: Runner | None = None,
    status_path: Path | None = None,
) -> dict[str, Any]:
    """Return structured scheduler health without running or changing the task."""

    payload = _base_payload()
    platform_value = str(platform_name or sys.platform).lower()
    if platform_value.startswith("linux"):
        return _linux_scheduler_status(
            payload=payload,
            project_root=Path(project_root),
            status_path=status_path,
        )
    if not platform_value.startswith("win"):
        payload.update(
            {
                "status": "not_supported",
                "reason_codes": ["windows_task_scheduler_not_available"],
            }
        )
        return payload

    run = runner or subprocess.run
    command = _probe_command(TASK_NAME)
    try:
        completed = run(
            command,
            cwd=Path(project_root),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        payload.update(
            {
                "ok": False,
                "status": "unavailable",
                "reason_codes": ["scheduled_task_probe_failed"],
                "error": f"{type(exc).__name__}:{exc}"[:1000],
            }
        )
        return payload

    if int(getattr(completed, "returncode", 1) or 0) != 0:
        payload.update(
            {
                "ok": False,
                "status": "unavailable",
                "reason_codes": ["scheduled_task_probe_failed"],
                "error": str(getattr(completed, "stderr", "") or "")[:1000],
            }
        )
        return payload
    try:
        raw = json.loads(str(getattr(completed, "stdout", "") or "{}"))
    except json.JSONDecodeError as exc:
        payload.update(
            {
                "ok": False,
                "status": "unavailable",
                "reason_codes": ["scheduled_task_probe_invalid_json"],
                "error": str(exc)[:1000],
            }
        )
        return payload
    if not isinstance(raw, dict):
        raw = {}

    exists = raw.get("exists") is True
    managed = raw.get("managed") is True
    enabled = raw.get("enabled") is True
    state = str(raw.get("state") or "")
    result_presence_flag = raw.get("last_task_result_present")
    if isinstance(result_presence_flag, bool):
        last_result_present = result_presence_flag
    else:
        last_result_present = (
            "last_task_result" in raw and raw.get("last_task_result") is not None
        )
    raw_last_result = raw.get("last_task_result")
    last_result_valid = bool(
        last_result_present
        and isinstance(raw_last_result, int)
        and not isinstance(raw_last_result, bool)
    )
    last_result = int(raw_last_result) if last_result_valid else None
    first_run_pending = bool(
        exists
        and last_result_valid
        and (
            last_result == FIRST_RUN_PENDING_RESULT
            or str(raw.get("last_run_time") or "").startswith("1999-11-30")
        )
    )
    action_contract_kind = (
        _action_contract_kind(raw, Path(project_root)) if exists else ""
    )
    action_contract_matches = bool(action_contract_kind)
    # A task with no enabled trigger never fires naturally, so it must not be
    # reported as healthy just because it exists with a valid action. Older
    # probe payloads omit the counts entirely; treat that as "unknown" rather
    # than as a failure so pre-existing callers keep working.
    trigger_count = _optional_int(raw.get("trigger_count"))
    enabled_trigger_count = _optional_int(raw.get("enabled_trigger_count"))
    trigger_known = trigger_count is not None or enabled_trigger_count is not None
    has_enabled_trigger = (
        (enabled_trigger_count if enabled_trigger_count is not None else trigger_count or 0) > 0
    )
    trigger_missing = bool(exists and trigger_known and not has_enabled_trigger)
    configured = bool(
        exists
        and managed
        and enabled
        and action_contract_matches
        and not trigger_missing
    )
    reason_codes: list[str] = []
    if not exists:
        status = "missing"
        reason_codes.append("managed_daily_data_task_missing")
    elif not managed:
        status = "unmanaged"
        reason_codes.append("scheduled_task_ownership_mismatch")
    elif not enabled:
        status = "disabled"
        reason_codes.append("scheduled_daily_data_task_disabled")
    elif not action_contract_matches:
        status = "misconfigured"
        reason_codes.append("scheduled_task_action_mismatch")
    elif trigger_missing:
        status = "misconfigured"
        reason_codes.append("scheduled_task_trigger_missing")
    elif state.lower() == "running":
        status = "running"
    elif first_run_pending:
        status = "ready_first_run_pending"
    elif not last_result_valid:
        status = "last_run_unknown"
    elif last_result == 0:
        status = "healthy"
    else:
        status = "last_run_failed"
        reason_codes.append("scheduled_chain_last_run_failed")

    if exists and not last_result_valid:
        if not last_result_present or raw_last_result is None:
            reason_codes.append("scheduled_task_last_result_missing")
        else:
            reason_codes.append("scheduled_task_last_result_invalid")

    payload.update(
        {
            "status": status,
            "exists": exists,
            "managed": managed,
            "enabled": enabled,
            "state": state,
            "action_contract_matches": action_contract_matches,
            "action_contract_kind": action_contract_kind,
            "trigger_count": trigger_count,
            "enabled_trigger_count": enabled_trigger_count,
            "has_enabled_trigger": has_enabled_trigger if trigger_known else None,
            "automatic_update_configured": configured,
            "first_run_pending": first_run_pending,
            "last_run_succeeded": bool(
                configured
                and last_result_valid
                and not first_run_pending
                and last_result == 0
            ),
            "last_run_time": str(raw.get("last_run_time") or ""),
            "last_task_result": last_result,
            "last_task_result_hex": (
                f"0x{last_result & 0xFFFFFFFF:08X}"
                if last_result is not None
                else ""
            ),
            "next_run_time": str(raw.get("next_run_time") or ""),
            "execute": str(raw.get("execute") or ""),
            "arguments": str(raw.get("arguments") or ""),
            "working_directory": str(raw.get("working_directory") or ""),
            "reason_codes": reason_codes,
        }
    )
    return payload


__all__ = [
    "LINUX_STATUS_SCHEMA",
    "LINUX_TASK_ID",
    "daily_data_scheduler_status",
]
