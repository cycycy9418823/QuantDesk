"""Owner pause/resume for writers only. Readers stay on.

``qd pause`` stops the Data Close Pipeline and the 20:20 AI trader.
The snapshot collector is paused if that task exists. Old disabled data
tasks are left disabled. Resume only re-enables what this command paused.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from quantdesk.shared import now_text
from quantdesk.utils import atomic_write_json

WRITER_TASKS = (
    "QuantDesk Data Close Pipeline",
    "QuantDesk AI Trader Evening",
    "QuantDesk Snapshot Collector",
)
STATE_RELATIVE = Path("state") / "desk_control.json"
DISCLAIMER = "纸面 · 未验证"


def _query_task(name: str, runner: Callable[..., subprocess.CompletedProcess[str]]) -> dict[str, Any]:
    completed = runner(
        ["schtasks", "/Query", "/TN", name, "/FO", "LIST"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return {"name": name, "exists": False, "state": "missing", "raw": completed.stderr.strip()}
    state = "unknown"
    for line in (completed.stdout or "").splitlines():
        if "Status:" in line or "状态:" in line or "Status：" in line:
            state = line.split(":", 1)[-1].strip() or "unknown"
            break
    lowered = (completed.stdout or "").lower()
    if "disabled" in lowered or "已禁用" in (completed.stdout or ""):
        state = "Disabled"
    elif "ready" in lowered or "就绪" in (completed.stdout or ""):
        state = "Ready"
    elif "running" in lowered:
        state = "Running"
    return {"name": name, "exists": True, "state": state, "raw": ""}


def _set_task(name: str, enable: bool, runner: Callable[..., subprocess.CompletedProcess[str]]) -> dict[str, Any]:
    flag = "/ENABLE" if enable else "/DISABLE"
    completed = runner(
        ["schtasks", "/Change", "/TN", name, flag],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "name": name,
        "ok": completed.returncode == 0,
        "action": "enable" if enable else "disable",
        "error": "" if completed.returncode == 0 else (completed.stderr or completed.stdout or "schtasks_failed").strip(),
    }


def _end_task(name: str, runner: Callable[..., subprocess.CompletedProcess[str]]) -> dict[str, Any]:
    completed = runner(
        ["schtasks", "/End", "/TN", name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "name": name,
        "ok": completed.returncode == 0,
        "action": "end",
        "error": "" if completed.returncode == 0 else (completed.stderr or completed.stdout or "schtasks_end_failed").strip(),
    }


def _load_pause_record(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"corrupt": True}
    if not isinstance(payload, dict):
        return {"corrupt": True}
    return payload


def _state_path(root: Path) -> Path:
    return Path(root) / STATE_RELATIVE


def pause_writers(
    project_root: str | Path,
    *,
    apply: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Disable writer tasks. Missing tasks are noted, not invented."""

    root = Path(project_root)
    run = runner or subprocess.run
    queried = [_query_task(name, run) for name in WRITER_TASKS]
    path = _state_path(root)
    previous = _load_pause_record(path)
    previous_names = [
        str(item)
        for item in (previous.get("paused_tasks") or [])
        if str(item).strip() and not previous.get("corrupt")
    ]
    paused: list[str] = list(previous_names)
    skipped: list[str] = []
    failed: list[str] = []
    actions: list[dict[str, Any]] = []
    writer_stops: list[dict[str, str]] = []
    if apply:
        for item in queried:
            name = str(item["name"])
            if not item["exists"]:
                skipped.append(name)
                continue
            state = str(item["state"] or "")
            if state.lower() == "disabled":
                skipped.append(name)
                continue
            if state.lower() == "running":
                ended = _end_task(name, run)
                actions.append(ended)
                if not ended["ok"]:
                    failed.append(name)
                    writer_stops.append({"name": name, "stop": "failed", "detail": ended.get("error") or ""})
                    continue
                writer_stops.append({"name": name, "stop": "stopped"})
            else:
                writer_stops.append({"name": name, "stop": "not_running_schedule_disabled"})
            result = _set_task(name, False, run)
            actions.append(result)
            if result["ok"]:
                if name not in paused:
                    paused.append(name)
            else:
                failed.append(name)
        atomic_write_json(
            path,
            {
                "schema": "desk_control_v1",
                "paused_at": str(previous.get("paused_at") or now_text()),
                "updated_at": now_text(),
                "paused_tasks": paused,
                "disclaimer": DISCLAIMER,
            },
        )
    payload = {
        "ok": not failed,
        "command": "pause",
        "apply": bool(apply),
        "disclaimer": DISCLAIMER,
        "writers": queried,
        "paused_tasks": paused,
        "failed_tasks": failed,
        "writer_stops": writer_stops,
        "skipped_missing_or_already_disabled": skipped,
        "actions": actions,
        "readers_left_on": True,
        "note": "只停写手和 20:20 AI。正在跑的会先结束再禁用下次调度。早报、复盘等读者任务不动。暂停期间缺的数据就是缺，恢复后不补。",
    }
    return payload


def resume_writers(
    project_root: str | Path,
    *,
    apply: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Re-enable only tasks this command previously paused."""

    root = Path(project_root)
    run = runner or subprocess.run
    path = _state_path(root)
    stored = _load_pause_record(path)
    names = [str(item) for item in (stored.get("paused_tasks") or []) if str(item).strip()]
    if stored.get("corrupt") or not names:
        return {
            "ok": False,
            "command": "resume",
            "apply": bool(apply),
            "disclaimer": DISCLAIMER,
            "resumed_tasks": [],
            "actions": [],
            "reason": "no_pause_record" if not stored.get("corrupt") else "pause_record_corrupt",
            "note": "没有可用的暂停清单，不会打开任何写手。旧的已禁用数据任务保持禁用。",
        }
    resumed: list[str] = []
    failed: list[str] = []
    actions: list[dict[str, Any]] = []
    if apply:
        remaining = list(names)
        for name in names:
            result = _set_task(name, True, run)
            actions.append(result)
            if result["ok"]:
                resumed.append(name)
                remaining = [item for item in remaining if item != name]
            else:
                failed.append(name)
        if failed:
            atomic_write_json(
                path,
                {
                    "schema": "desk_control_v1",
                    "paused_at": stored.get("paused_at") or now_text(),
                    "updated_at": now_text(),
                    "paused_tasks": remaining,
                    "resume_failed_tasks": failed,
                    "disclaimer": DISCLAIMER,
                },
            )
        elif path.is_file():
            path.unlink()
    return {
        "ok": not failed,
        "command": "resume",
        "apply": bool(apply),
        "disclaimer": DISCLAIMER,
        "resumed_tasks": resumed,
        "failed_tasks": failed,
        "actions": actions,
        "note": "只恢复上次 pause 停掉的写手。旧的已禁用数据任务不会被重新打开。失败时保留暂停记录。",
    }
