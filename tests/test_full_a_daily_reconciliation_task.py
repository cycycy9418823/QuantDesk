"""Contract tests for the full-A daily reconciliation task surface.

The CLI, the .cmd launcher and the PowerShell installer must stay consistent
with each other and with the coordinator they drive. A scheduled task that
silently stops matching its runner is the failure mode these tests exist to
catch.

Nothing here registers a real task, calls a provider, touches the network or
writes canonical data: the installer is only ever invoked with -Action Plan.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PY_RUNNER = ROOT / "scripts/run_full_a_daily_reconciliation.py"
CMD_RUNNER = ROOT / "scripts/run_full_a_daily_reconciliation.cmd"
INSTALLER = ROOT / "scripts/install_full_a_daily_reconciliation_task.ps1"


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return (raw or b"").decode(encoding)
        except UnicodeDecodeError:
            continue
    return (raw or b"").decode("utf-8", errors="replace")


def _load_cli():
    """Import the runner by path; scripts/ is not an importable package."""
    spec = importlib.util.spec_from_file_location("_full_a_cli", PY_RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _powershell_json(args: list[str]) -> dict:
    if shutil.which("powershell") is None:
        pytest.skip("powershell unavailable")
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, _decode(completed.stderr)
    return json.loads(_decode(completed.stdout))


def test_task_surface_files_exist() -> None:
    assert PY_RUNNER.is_file()
    assert CMD_RUNNER.is_file()
    assert INSTALLER.is_file()


def test_cli_defaults_are_offline_and_non_applying() -> None:
    cli = _load_cli()
    args = cli.build_parser().parse_args([])
    assert args.enable_network is False
    assert args.network == "false"
    assert args.apply == ""


def test_cli_accepts_the_flags_the_installer_passes() -> None:
    cli = _load_cli()
    args = cli.build_parser().parse_args(
        ["--enable-network", "--apply", "--lock-timeout-seconds", "1800", "--json", "false"]
    )
    assert args.enable_network is True
    assert args.apply == "true"
    assert args.lock_timeout_seconds == pytest.approx(1800.0)
    assert args.json == "false"


def test_default_target_date_is_a_complete_session() -> None:
    """A calendar-day trigger must not invent a date the market never traded."""
    cli = _load_cli()
    from quantdesk.trading_calendar import is_a_share_trading_day

    target = cli.default_target_date()
    assert target, "default target date must not be empty"
    assert is_a_share_trading_day(target) is True


def test_exit_codes_separate_deferral_partial_and_blocked() -> None:
    cli = _load_cli()
    assert cli.exit_code_for("completed") == 0
    assert cli.exit_code_for("planned_offline") == 0
    assert cli.exit_code_for("skipped_existing_run") == 0
    assert cli.exit_code_for("deferred_upstream_busy") == 3
    assert cli.exit_code_for("partial") == 4
    assert cli.exit_code_for("blocked") == 5
    # An unrecognised status must never read as success.
    assert cli.exit_code_for("something_new") == 5
    assert cli.exit_code_for("") == 5


def test_launcher_invokes_the_cli_and_forwards_arguments() -> None:
    text = CMD_RUNNER.read_text(encoding="utf-8", errors="replace")
    assert "run_full_a_daily_reconciliation.py" in text
    assert "%*" in text, "launcher must forward the task's arguments"
    assert "-B" in text, "launcher must ignore stale bytecode"


def test_launcher_and_installer_carry_no_sendkey() -> None:
    """No credential may be set or read here.

    Checked as real usage rather than as a bare word: both files legitimately
    document in prose that they carry no SendKey, and a substring scan would
    flag that denial as if it were the violation.
    """
    usage_patterns = (
        "set sendkey", 'set "sendkey', "sendkey=",
        "$env:quantdesk_serverchan", "%quantdesk_serverchan",
        "$sendkey", "-sendkey",
    )
    for path in (CMD_RUNNER, INSTALLER):
        lowered = path.read_text(encoding="utf-8", errors="replace").lower()
        for pattern in usage_patterns:
            assert pattern not in lowered, f"{path.name} appears to handle a credential: {pattern!r}"


def test_installer_plan_registers_two_weekday_triggers() -> None:
    payload = _powershell_json(
        ["-File", str(INSTALLER), "-Action", "Plan", "-Json"]
    )
    assert payload["action"] == "Plan"
    triggers = payload.get("triggers") or []
    joined = " ".join(str(t) for t in triggers)
    assert "20:35" in joined
    assert "22:30" in joined
    assert "Mon-Fri" in joined


def test_installer_plan_authorizes_network_and_apply() -> None:
    """A reconciliation that may not write cannot close its own contract."""
    payload = _powershell_json(
        ["-File", str(INSTALLER), "-Action", "Plan", "-Json"]
    )
    arguments = str(payload.get("arguments") or "")
    assert "--enable-network" in arguments
    assert "--apply" in arguments
    assert "run_full_a_daily_reconciliation.cmd" in arguments


def test_installer_plan_does_not_touch_the_legacy_saturday_task() -> None:
    """Retiring the weekly Saturday task is gated behind a natural run."""
    payload = _powershell_json(
        ["-File", str(INSTALLER), "-Action", "Plan", "-Json"]
    )
    assert payload.get("touches_legacy_saturday_task") is False
    text = INSTALLER.read_text(encoding="utf-8", errors="replace")
    assert "Weekly Daily Wide Pool" not in text.replace(
        "does NOT touch the legacy weekly Saturday task", ""
    )


def test_installer_plan_is_read_only() -> None:
    payload = _powershell_json(
        ["-File", str(INSTALLER), "-Action", "Plan", "-Json"]
    )
    assert "nothing was registered" in str(payload.get("note") or "").lower()
