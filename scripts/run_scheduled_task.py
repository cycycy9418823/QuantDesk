"""Run an existing Windows task quietly, respecting the local exchange calendar."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from quantdesk.trading_calendar import calendar_info, is_trade_day  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")


def decode_action(encoded: str) -> dict[str, str]:
    """Decode the original task action without interpreting its arguments."""
    value = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    keys = {"executable", "arguments", "working_directory"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid_action_fields")
    if any(not isinstance(value[k], str) or "\x00" in value[k] for k in keys):
        raise ValueError("invalid_action_values")
    if not value["executable"].strip() or not value["working_directory"].strip():
        raise ValueError("missing_action_location")
    return value


def task_log_path(root: Path, name: str) -> Path:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:70] or "task"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return root / "logs" / "ops" / "scheduled_tasks" / f"{slug}-{digest}.log"


def child_options(action: dict[str, str], handle: object) -> tuple[object, dict]:
    """Keep the exact Windows argument string and give descendants a hidden console."""
    command = subprocess.list2cmdline([action["executable"]])
    if action["arguments"]:
        command += " " + action["arguments"]
    options = dict(cwd=action["working_directory"], shell=False,
                   stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT)
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options.update(startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        command = [action["executable"], *shlex.split(action["arguments"])]
    return command, options


def run_task(root: Path, name: str, policy: str, action: dict[str, str], *,
             now: datetime | None = None, runner: Callable | None = None) -> int:
    """Skip known market closures before starting any business process."""
    root = Path(root)
    current = now or datetime.now(SHANGHAI)
    current = current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)
    if policy not in {"trading_day", "daily"}:
        raise ValueError("invalid_schedule_policy")
    log = task_log_path(root, name)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8", buffering=1) as handle:
        def record(status: str, **values: object) -> None:
            handle.write(json.dumps({"at": datetime.now(SHANGHAI).isoformat(),
                "task": name, "policy": policy, "date": current.date().isoformat(),
                "status": status, **values}, ensure_ascii=False) + "\n")
            handle.flush()
        try:
            if policy == "trading_day":
                path = root / "data/calendar/a_share_trading_calendar.csv"
                info = calendar_info(path, target_date=current.date())
                warning = info.get("coverage_warning") or ""
                if warning:
                    record("calendar_warning", warning=warning,
                           calendar_quality=info.get("calendar_quality"))
                if not is_trade_day(current.date(), calendar_path=path):
                    record("skipped_non_trading_day", exit_code=0,
                           calendar_quality=info.get("calendar_quality"), warning=warning)
                    return 0
            record("started")
            command, options = child_options(action, handle)
            result = (runner or subprocess.run)(command, **options)
            code = int(result.returncode)
            record("completed" if code == 0 else "failed", exit_code=code)
            return code
        except Exception as exc:
            record("launcher_failed", error_type=type(exc).__name__, exit_code=2)
            return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--policy", choices=("trading_day", "daily"), required=True)
    parser.add_argument("--action-base64", required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)
    try:
        action = decode_action(args.action_base64)
    except Exception as exc:
        log = task_log_path(args.project_root, args.task_name)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"status": "invalid_action", "error_type": type(exc).__name__,
                                     "exit_code": 2}) + "\n")
        return 2
    return run_task(args.project_root, args.task_name, args.policy, action)


if __name__ == "__main__":
    raise SystemExit(main())
