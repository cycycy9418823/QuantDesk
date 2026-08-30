from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.intraday_live_engine import init_intraday_live_account_state
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def default_user_config_path() -> Path:
    return Path.home() / ".quantdesk" / "config.yaml"


def default_data_dir() -> Path:
    return Path.home() / "QuantDeskData"


def render_user_config(
    *,
    data_dir: str | Path,
    install_sample_data: bool,
    network_enabled: bool,
    profile: str,
    schedule_daily_1600: bool,
) -> str:
    return "\n".join(
        [
            f'app_version: "{APP_VERSION}"',
            f'api_schema_version: "{API_SCHEMA_VERSION}"',
            f'data_dir: "{Path(data_dir).expanduser()}"',
            f"install_sample_data: {str(bool(install_sample_data)).lower()}",
            f"network_enabled: {str(bool(network_enabled)).lower()}",
            f'profile: "{profile}"',
            f"schedule_daily_1600: {str(bool(schedule_daily_1600)).lower()}",
            "research_only: true",
            "no_broker: true",
            "no_real_orders: true",
            "disclaimer: \"Local research diagnostics only. Not investment advice.\"",
            "",
        ]
    )


def write_user_config(
    path: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
    install_sample_data: bool = True,
    network_enabled: bool = False,
    profile: str = "both",
    schedule_daily_1600: bool = False,
) -> dict[str, Any]:
    config_path = Path(path).expanduser() if path else default_user_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    selected_data_dir = Path(data_dir).expanduser() if data_dir else default_data_dir()
    selected_data_dir.mkdir(parents=True, exist_ok=True)
    text = render_user_config(
        data_dir=selected_data_dir,
        install_sample_data=install_sample_data,
        network_enabled=network_enabled,
        profile=profile,
        schedule_daily_1600=schedule_daily_1600,
    )
    config_path.write_text(text, encoding="utf-8")
    return {
        "config_path": str(config_path),
        "data_dir": str(selected_data_dir),
        "network_enabled": bool(network_enabled),
        "install_sample_data": bool(install_sample_data),
        "profile": profile,
        "schedule_daily_1600": bool(schedule_daily_1600),
    }


def run_self_check_smoke(*, enabled: bool = True) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False, "status": "skipped", "warnings": ["self_check skipped by operator flag"]}
    completed = subprocess.run(  # noqa: S603 - local project script only
        [sys.executable, "scripts/self_check.py"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    last_line = (completed.stdout or "").strip().splitlines()[-1:] or [""]
    summary: dict[str, Any] = {}
    try:
        summary = json.loads(last_line[0]) if last_line[0] else {}
    except json.JSONDecodeError:
        summary = {"parse_warning": "self_check summary JSON not found"}
    return {
        "enabled": True,
        "status": "passed" if completed.returncode == 0 else "warning",
        "returncode": completed.returncode,
        "summary": summary,
        "warnings": [] if completed.returncode == 0 else ["self_check returned non-zero; inspect stdout/stderr"],
    }


def run_first_run_setup(
    *,
    config_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    install_sample_data: bool = True,
    network_enabled: bool = False,
    profile: str = "both",
    schedule_daily_1600: bool = False,
    init_live_account: bool = True,
    force_live_init: bool = False,
    run_self_check: bool = True,
) -> dict[str, Any]:
    warnings: list[str] = []
    profile_value = profile if profile in {"long_term", "intraday_observer", "both"} else "both"
    if profile_value != profile:
        warnings.append("unknown profile; using both")
    config_result = write_user_config(
        config_path,
        data_dir=data_dir,
        install_sample_data=install_sample_data,
        network_enabled=network_enabled,
        profile=profile_value,
        schedule_daily_1600=schedule_daily_1600,
    )
    live_result: dict[str, Any] = {"enabled": False, "status": "skipped"}
    if init_live_account:
        live_result = init_intraday_live_account_state(force=force_live_init)
        if live_result.get("status") == "warning":
            warnings.extend(str(item) for item in live_result.get("warnings") or [])
    scheduler = {
        "requested": bool(schedule_daily_1600),
        "status": "not_created",
        "warning": "Windows Task Scheduler creation is optional and not performed by default.",
    }
    self_check = run_self_check_smoke(enabled=run_self_check)
    warnings.extend(str(item) for item in self_check.get("warnings") or [])
    payload = {
        "ok": True,
        "status": "warning" if warnings else "passed",
        "task_type": "first_run_wizard",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "network": bool(network_enabled),
        "config": config_result,
        "sample_data": {
            "install_requested": bool(install_sample_data),
            "manifest": "assets/sample_data_manifest.json",
            "synthetic_sample_data": True,
        },
        "intraday_live_account": live_result,
        "scheduler": scheduler,
        "self_check": self_check,
        "warnings": warnings,
        "next_commands": [
            "python scripts/qd.py daily --network false --with-guardrails true --summary true",
            "python scripts/qd.py live --once --summary true",
            "python scripts/qd.py risk-attribution --summary true",
            "python scripts/qd.py experiments --summary true",
        ],
        "disclaimer": "First-run setup is research-only. No broker connection. No real orders. Network remains disabled unless explicitly enabled.",
    }
    return payload


def build_first_run_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(payload.get("ok")),
        "status": payload.get("status"),
        "task_type": payload.get("task_type"),
        "generated_at": payload.get("generated_at"),
        "app_version": payload.get("app_version"),
        "network": payload.get("network"),
        "config_path": (payload.get("config") or {}).get("config_path"),
        "data_dir": (payload.get("config") or {}).get("data_dir"),
        "sample_data_manifest": (payload.get("sample_data") or {}).get("manifest"),
        "live_account_status": (payload.get("intraday_live_account") or {}).get("status"),
        "self_check_status": (payload.get("self_check") or {}).get("status"),
        "warning_count": len(payload.get("warnings") or []),
        "warnings_top": list(payload.get("warnings") or [])[:5],
        "next_commands": list(payload.get("next_commands") or [])[:4],
        "disclaimer": "Research-only first-run setup. No broker. No real orders.",
    }
