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
import uuid
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from quantdesk.trading_calendar import calendar_info, is_trade_day  # noqa: E402
from quantdesk.interprocess_lock import interprocess_file_lock  # noqa: E402

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


def source_fingerprint(root: Path) -> dict[str, object]:
    """Identify actual source bytes; never log task arguments or local secrets."""
    result: dict[str, object] = {"status": "wrapper_only", "git_head": None,
        "launcher_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if not (root / ".git").exists():
        return result
    options = {"capture_output": True, "timeout": 8, "check": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        head = subprocess.run(["git", "--no-optional-locks", "-C", str(root), "rev-parse", "HEAD"], **options)
        names = subprocess.run(["git", "--no-optional-locks", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"], **options)
        entries: dict[str, str] = {}
        for name in sorted(set(names.stdout.decode("utf-8").split("\0")) - {""}):
            relative = Path(name)
            if (relative.is_absolute() or ".." in relative.parts or relative.parts[0] not in {"quantdesk", "scripts", "ai_trader", "app", "strategies"}
                    or relative.suffix.lower() not in {".py", ".js", ".html", ".css", ".yaml", ".yml"}):
                continue
            path = root / relative
            if path.is_file() and not path.is_symlink():
                entries[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        result.update(status="source_hashed", git_head=head.stdout.decode("ascii").strip(),
            source_file_count=len(entries), source_sha256=hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result.update(status="incomplete", warning=f"source_fingerprint_unavailable:{type(exc).__name__}")
    return result


def _protect_windows_descendants() -> None:
    # Reuse the tested non-inheritable KILL_ON_JOB_CLOSE implementation. The
    # OS owns handle cleanup; closing it while running would kill this process.
    if os.name == "nt":
        from scripts.run_data_close_pipeline import _protect_child_process_tree
        _protect_child_process_tree()


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_task(root: Path, name: str, policy: str, action: dict[str, str], *,
             now: datetime | None = None, runner: Callable | None = None,
             protector: Callable | None = None) -> int:
    """One launcher per task, with durable start/end receipts and child ownership."""
    root = Path(root)
    log = task_log_path(root, name)
    try:
        with interprocess_file_lock(log.with_suffix(".lock"), timeout_seconds=0):
            return _run_task_locked(root, name, policy, action, now=now, runner=runner, protector=protector)
    except TimeoutError:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": datetime.now(SHANGHAI).isoformat(), "task": name,
                "status": "already_running", "exit_code": 3}) + "\n")
        return 3


def _run_task_locked(root: Path, name: str, policy: str, action: dict[str, str], *,
             now: datetime | None = None, runner: Callable | None = None,
             protector: Callable | None = None) -> int:
    """Skip known market closures before starting any business process."""
    root = Path(root)
    current = now or datetime.now(SHANGHAI)
    current = current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)
    if policy not in {"trading_day", "daily"}:
        raise ValueError("invalid_schedule_policy")
    log = task_log_path(root, name)
    log.parent.mkdir(parents=True, exist_ok=True)
    receipt = log.with_suffix(".latest.json")
    run_id = str(uuid.uuid4())
    action_sha256 = hashlib.sha256(json.dumps(action, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    fingerprint: dict[str, object] = {}
    with log.open("a", encoding="utf-8", buffering=1) as handle:
        def record(status: str, **values: object) -> None:
            payload = {"at": datetime.now(SHANGHAI).isoformat(), "run_id": run_id, "pid": os.getpid(),
                "task": name, "policy": policy, "date": current.date().isoformat(),
                "status": status, "action_sha256": action_sha256, "code_fingerprint": fingerprint, **values}
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            if status in {"started", "completed", "failed", "launcher_failed", "interrupted", "skipped_non_trading_day"}:
                _write_receipt(receipt, payload)
        try:
            if receipt.exists():
                try:
                    previous = json.loads(receipt.read_text(encoding="utf-8"))
                    if previous.get("status") == "started":
                        record("previous_run_interrupted", previous_run_id=previous.get("run_id"),
                               previous_started_at=previous.get("at"), reason="task_lock_reacquired_without_completion_receipt")
                except (OSError, ValueError, AttributeError):
                    record("previous_receipt_unreadable", reason="no_completed_run_inferred")
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
            fingerprint = source_fingerprint(root)
            if protector is not None:
                protector()
            elif runner is None:
                _protect_windows_descendants()
            record("started", descendant_protection="injected_runner" if runner is not None else "windows_job" if os.name == "nt" else "not_available_on_this_platform")
            command, options = child_options(action, handle)
            result = (runner or subprocess.run)(command, **options)
            code = int(result.returncode)
            record("completed" if code == 0 else "failed", exit_code=code)
            return code
        except KeyboardInterrupt:
            record("interrupted", exit_code=130)
            return 130
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
