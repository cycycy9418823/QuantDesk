"""Owner pause/resume for writers only. Readers stay on.

``qd pause`` manages the writer inventory below: the close pipeline, both paper
AI profiles, intraday refresh, snapshot pull, the evening strategy-book review,
the weekly daily-wide pool and dated AI observation tasks (retired data tasks
are covered too in case one is re-enabled).  Task state is read in a
language-independent way: one ``Get-ScheduledTask`` call returns the State enum
name.  ``schtasks /Query`` is only the fallback; its output is decoded with the
console code page, so a Chinese Windows ("模式: 就绪") is understood.

A real pause ends a running task, kills the process tree it leaves behind
(including orphaned writer processes found by their exact script path), proves
the stop, and only then disables the next schedule.  Readers (backup, health
line, integrity check, morning brief) stay on.  Resume only re-enables what
this command paused.
"""

from __future__ import annotations

import base64
import csv
import ctypes
import io
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from quantdesk.shared import now_text
from quantdesk.utils import atomic_write_json

# The scheduled writers this switch owns, in stop order.
WRITER_TASKS = (
    "QuantDesk Data Close Pipeline",      # data/daily, 15:40 / 18:30
    "QuantDesk AI Trader Evening",        # state/ai_trader, 20:20
    "QuantDesk A0 Snapshot Collector",    # local data/snapshots (Disabled; student is primary)
    "QuantDesk AI Trader Evening (GLM)",  # state/ai_trader_glm, 20:40
    "QuantDesk Intraday Data Refresh",    # data/cache + data/intraday, every 5 min
    "QuantDesk Snapshot Pull",            # data/snapshots pulled from student
    "QuantDesk Daily Review Routine",     # state/auto_paper_portfolio.json, 20:02
    "QuantDesk Weekly Daily Wide Pool",   # data/daily wide pool, weekly
)
# Dated tasks such as "QuantDesk AI Observation 20261008" are matched by prefix.
WRITER_TASK_PREFIXES = ("QuantDesk AI Observation",)
# Retired data writers stay in the inventory: if one is ever re-enabled by
# mistake, pause still stops it.  Already-disabled ones are only reported.
RETIRED_WRITER_TASKS = (
    "QuantDesk Daily Data Refresh",
    "QuantDesk Daily Data Chain",
    "QuantDesk Full A Daily Reconciliation",
    "QuantDesk Universe Baseline Refresh",
    "QuantDesk Weekly Minute Wide Pool",
)
# Reader tasks are never touched (listed so the owner sees what keeps running).
READER_TASKS = (
    "QuantDesk Backup Push",
    "QuantDesk Health Line",
    "QuantDesk Daily Integrity Check",
    "QuantDesk Morning Brief",
)
STATE_RELATIVE = Path("state") / "desk_control.json"
DISCLAIMER = "纸面 · 未验证"
TASK_PREFIX = "QuantDesk"
SCHTASKS_TIMEOUT = 60
INVENTORY_TIMEOUT = 90
KILL_TIMEOUT = 30
STOP_POLLS = 5
STOP_POLL_SECONDS = 2.0
_sleep = time.sleep

RunFn = Callable[..., subprocess.CompletedProcess]
InventoryFn = Callable[[], dict]
KillFn = Callable[[int], dict]


# --------------------------------------------------------------------- decoding
def _console_codepages() -> list[str]:
    pages: list[str] = []
    if os.name == "nt":
        try:
            kernel = ctypes.windll.kernel32
            pages += [f"cp{page}" for page in (kernel.GetConsoleOutputCP(), kernel.GetOEMCP(), kernel.GetACP()) if page]
        except (AttributeError, OSError):
            pass
    pages.append("gbk")
    return list(dict.fromkeys(pages))


def _decodings(value: Any) -> list[str]:
    """Every clean decoding of scheduler output: UTF-8, then the console code pages.

    Short GBK text can also be valid UTF-8 by accident, so callers that parse
    the text pick the decoding in which they recognise a field.
    """

    if value is None:
        return [""]
    if isinstance(value, str):
        return [value]
    raw = bytes(value)
    found: list[str] = []
    for encoding in ("utf-8", *_console_codepages()):
        try:
            text = raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        if text not in found:
            found.append(text)
    return found or [raw.decode("utf-8", errors="replace")]


def _console_text(value: Any) -> str:
    return _decodings(value)[0]


_STATE_WORDS = {
    "ready": "Ready", "就绪": "Ready", "准备就绪": "Ready",
    "running": "Running", "正在运行": "Running", "运行中": "Running",
    "disabled": "Disabled", "已禁用": "Disabled", "禁用": "Disabled",
    "queued": "Queued", "已排队": "Queued", "排队": "Queued",
    "unknown": "Unknown", "未知": "Unknown",
}
# MSFT_ScheduledTask State enum, in case a host serialises the number.
_STATE_NUMBERS = {"0": "Unknown", "1": "Disabled", "2": "Queued", "3": "Ready", "4": "Running"}


def _normalize_state(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return "unknown"
    if text in _STATE_NUMBERS:
        return _STATE_NUMBERS[text]
    return _STATE_WORDS.get(text.lower(), text)


def _is_running_state(state: Any) -> bool:
    text = str(state or "").strip().lower()
    return text in {"running", "正在运行", "运行中"} or "running" in text or "正在运行" in text or "运行中" in text


def _is_queued_state(state: Any) -> bool:
    return _normalize_state(state) == "Queued"


def _is_stopped_state(state: Any) -> bool:
    text = str(state or "").strip().lower()
    return text in {"ready", "disabled", "就绪", "准备就绪", "已禁用"}


def _is_disabled_state(state: Any) -> bool:
    text = str(state or "").strip().lower()
    return text in {"disabled", "已禁用"} or "disabled" in text or "已禁用" in text


_MISSING_MARKERS = ("not found", "cannot find", "does not exist", "找不到", "不存在")
# "状态" must start the line: "登录状态:" (logon mode) is not the task state.
# zh-CN schtasks /FO LIST prints the state as "模式:".
_STATE_LINE = re.compile(r"^(?:status|state|状态|模式)\s*:\s*(.*?)\s*$", re.IGNORECASE)


_KNOWN_STATES = {"Ready", "Running", "Disabled", "Queued", "Unknown"}


def _run_schtasks(run: RunFn, argv: list[str]) -> tuple[int, Any, Any]:
    """Run one schtasks command; output stays raw so the caller can pick the decoding."""

    try:
        completed = run(argv, capture_output=True, timeout=SCHTASKS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 1, "", "scheduler_query_timeout"
    except OSError as exc:
        return 1, "", f"scheduler_unavailable:{type(exc).__name__}"
    return int(completed.returncode), completed.stdout, completed.stderr


def _error_choices(stdout: Any, stderr: Any) -> list[str]:
    source = stderr if _console_text(stderr).strip() else stdout
    return [text.strip() for text in _decodings(source)]


def _is_missing_error(choices: list[str]) -> bool:
    return any(marker in text.lower() for text in choices for marker in _MISSING_MARKERS)


def _state_from_list(text: str) -> str | None:
    for line in text.splitlines():
        match = _STATE_LINE.match(line.replace("：", ":").strip())
        if match:
            return _normalize_state(match.group(1))
    return None


def _query_task(name: str, runner: RunFn) -> dict[str, Any]:
    code, stdout, stderr = _run_schtasks(runner, ["schtasks", "/Query", "/TN", name, "/FO", "LIST"])
    if code != 0:
        choices = _error_choices(stdout, stderr)
        missing = _is_missing_error(choices)
        raw_error = next((text for text in choices if _is_missing_error([text])), choices[0] if choices else "")
        return {
            "name": name,
            "exists": not missing,
            "state": "missing" if missing else "unknown",
            "query_failed": not missing,
            "error": raw_error,
            "raw": raw_error,
        }
    state = "unknown"
    fallback = None
    for text in _decodings(stdout):
        found = _state_from_list(text)
        if found in _KNOWN_STATES:
            state = found
            break
        if found is not None and fallback is None:
            fallback = found
    else:
        if fallback is not None:
            state = fallback
    return {"name": name, "exists": True, "state": state, "query_failed": False, "error": "", "raw": ""}


def _list_prefixed_tasks(runner: RunFn, prefixes: tuple[str, ...]) -> tuple[list[str], str]:
    """Fallback discovery of dated writer tasks through ``schtasks /Query /FO CSV``."""

    code, stdout, stderr = _run_schtasks(runner, ["schtasks", "/Query", "/FO", "CSV", "/NH"])
    if code != 0:
        choices = _error_choices(stdout, stderr)
        if _is_missing_error(choices):
            return [], ""
        return [], (choices[0] if choices else "") or "scheduler_list_failed"
    names: list[str] = []
    for row in csv.reader(io.StringIO(_console_text(stdout))):
        if not row:
            continue
        name = row[0].strip().lstrip("\\")
        if "\\" in name:
            continue  # only root-folder tasks are managed here
        if any(name.startswith(prefix) for prefix in prefixes) and name not in names:
            names.append(name)
    return names, ""


def _set_task(name: str, enable: bool, runner: RunFn) -> dict[str, Any]:
    flag = "/ENABLE" if enable else "/DISABLE"
    code, stdout, stderr = _run_schtasks(runner, ["schtasks", "/Change", "/TN", name, flag])
    return {
        "name": name,
        "ok": code == 0,
        "action": "enable" if enable else "disable",
        "error": "" if code == 0 else ((_error_choices(stdout, stderr) or [""])[0] or "schtasks_failed"),
    }


def _end_task(name: str, runner: RunFn) -> dict[str, Any]:
    code, stdout, stderr = _run_schtasks(runner, ["schtasks", "/End", "/TN", name])
    return {
        "name": name,
        "ok": code == 0,
        "action": "end",
        "error": "" if code == 0 else ((_error_choices(stdout, stderr) or [""])[0] or "schtasks_end_failed"),
    }


# ------------------------------------------------------------ inventory (read-only)
# One read-only PowerShell call: task State as an enum *name* (language
# independent), each task's actions, the scheduler's running instances (with the
# engine PID) and the process table.  Nothing here starts, stops or changes a task.
_INVENTORY_SCRIPT = r"""$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$tasks = @(Get-ScheduledTask -TaskPath '\' | Where-Object { $_.TaskName -like 'QuantDesk*' } | ForEach-Object {
    [pscustomobject]@{
        name = [string]$_.TaskName
        path = [string]$_.TaskPath
        state = [string]$_.State
        actions = @($_.Actions | ForEach-Object { [pscustomobject]@{ execute = [string]$_.Execute; arguments = [string]$_.Arguments } })
    }
})
$running = $null
try {
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $running = @(foreach ($item in $service.GetRunningTasks(1)) { [pscustomobject]@{ path = [string]$item.Path; engine_pid = [int]$item.EnginePID } })
} catch { $running = $null }
$processes = @(Get-CimInstance Win32_Process | ForEach-Object {
    $created = ''
    if ($_.CreationDate) { $created = $_.CreationDate.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ') }
    [pscustomobject]@{ pid = [int]$_.ProcessId; ppid = [int]$_.ParentProcessId; name = [string]$_.Name; created = $created; command_line = [string]$_.CommandLine }
})
[pscustomobject]@{ tasks = $tasks; running = $running; processes = $processes; shell_pid = $PID } | ConvertTo-Json -Depth 6 -Compress
"""


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def read_scheduler_inventory() -> dict[str, Any]:
    """Real, read-only inventory.  Raises OSError on any failure (caller falls back)."""

    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _INVENTORY_SCRIPT],
            capture_output=True,
            timeout=INVENTORY_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError("scheduler_inventory_timeout") from exc
    if completed.returncode != 0:
        raise OSError("scheduler_inventory_failed:" + _console_text(completed.stderr)[-300:])
    try:
        payload = json.loads(_console_text(completed.stdout).lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise OSError("scheduler_inventory_unparsable") from exc
    if not isinstance(payload, dict):
        raise OSError("scheduler_inventory_unparsable")
    return payload


_SCRIPT_PATH = re.compile(r"[A-Za-z]:\\[^\"'\s<>|*?]+?\.(?:py|cmd|bat|ps1)(?![\w.])", re.IGNORECASE)
_LAUNCHER = "run_scheduled_task.py"
_ROOT_PROCESS_NAMES = {"python.exe", "pythonw.exe", "cmd.exe", "powershell.exe", "pwsh.exe"}


def _norm_cmd(text: Any) -> str:
    return str(text or "").replace("/", "\\").lower()


def _task_markers(task: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(script paths, launcher task-name markers) that identify this task's processes."""

    paths: list[str] = []
    launcher: list[str] = []

    def add_paths(text: str) -> None:
        for match in _SCRIPT_PATH.finditer(text or ""):
            path = _norm_cmd(match.group(0))
            if path.endswith(_LAUNCHER):
                continue
            if path not in paths:
                paths.append(path)

    for action in _as_list(task.get("actions")):
        if not isinstance(action, dict):
            continue
        arguments = str(action.get("arguments") or "")
        if _LAUNCHER in _norm_cmd(arguments):
            launcher.append(f'--task-name "{str(task.get("name") or "").lower()}"')
            encoded = re.search(r"--action-base64\s+(\S+)", arguments)
            if encoded:
                try:
                    inner = json.loads(base64.b64decode(encoded.group(1), validate=True).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    inner = {}
                if isinstance(inner, dict):
                    add_paths(str(inner.get("arguments") or ""))
        else:
            add_paths(arguments)
    return paths, launcher


def _contains_marker(command: str, marker: str) -> bool:
    start = command.find(marker)
    while start >= 0:
        end = start + len(marker)
        if end >= len(command) or not (command[end].isalnum() or command[end] in "._-"):
            return True
        start = command.find(marker, start + 1)
    return False


def _own_lineage(processes: list[dict[str, Any]], shell_pid: Any) -> set[int]:
    """This process, its ancestors and the inventory shell are never kill targets."""

    by_pid = {int(p.get("pid") or 0): p for p in processes if isinstance(p, dict)}
    excluded = {os.getpid()}
    try:
        excluded.add(int(shell_pid))
    except (TypeError, ValueError):
        pass
    current = by_pid.get(os.getpid())
    guard = 0
    while current is not None and guard < 64:
        parent = int(current.get("ppid") or 0)
        if parent <= 0 or parent in excluded:
            break
        excluded.add(parent)
        current = by_pid.get(parent)
        guard += 1
    return excluded


def _writer_processes(task: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Processes that belong to one writer task: engine root, launcher, script, descendants."""

    processes = [p for p in _as_list(snapshot.get("processes")) if isinstance(p, dict)]
    excluded = _own_lineage(processes, snapshot.get("shell_pid"))
    paths, launcher = _task_markers(task)
    task_path = "\\" + str(task.get("name") or "")
    engine = {
        int(item.get("engine_pid") or 0)
        for item in _as_list(snapshot.get("running"))
        if isinstance(item, dict) and str(item.get("path") or "").lower() == task_path.lower()
    }
    roots: dict[int, str] = {}
    for proc in processes:
        pid = int(proc.get("pid") or 0)
        if pid <= 0 or pid in excluded:
            continue
        if pid in engine:
            roots[pid] = "scheduler_engine"
            continue
        if str(proc.get("name") or "").lower() not in _ROOT_PROCESS_NAMES:
            continue
        command = _norm_cmd(proc.get("command_line"))
        if launcher and _LAUNCHER in command and any(_contains_marker(command, m) for m in launcher):
            roots[pid] = "launcher_command_line"
        elif any(_contains_marker(command, m) for m in paths):
            roots[pid] = "script_path"
    matched = dict(roots)
    created = {int(p.get("pid") or 0): str(p.get("created") or "") for p in processes}
    changed = True
    while changed:
        changed = False
        for proc in processes:
            pid = int(proc.get("pid") or 0)
            parent = int(proc.get("ppid") or 0)
            if pid in matched or pid in excluded or parent not in matched:
                continue
            # A recycled parent PID is not a parent: the child must be younger.
            if created.get(pid) and created.get(parent) and created[pid] < created[parent]:
                continue
            matched[pid] = "descendant"
            changed = True
    rows = []
    for proc in processes:
        pid = int(proc.get("pid") or 0)
        if pid not in matched:
            continue
        command = str(proc.get("command_line") or "")
        command = re.sub(r"(--action-base64\s+)\S+", r"\1…", command)
        rows.append({
            "pid": pid,
            "ppid": int(proc.get("ppid") or 0),
            "name": str(proc.get("name") or ""),
            "created": created.get(pid, ""),
            "matched_by": matched[pid],
            "top_level": int(proc.get("ppid") or 0) not in matched,
            "command_line": command[:240],
        })
    return rows


def _inventory_writers(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = {}
    for task in _as_list(snapshot.get("tasks")):
        if isinstance(task, dict) and str(task.get("name") or ""):
            tasks.setdefault(str(task["name"]), task)
    process_check = isinstance(snapshot.get("processes"), list) and len(snapshot["processes"]) > 0
    running_known = isinstance(snapshot.get("running"), list)
    wanted = [(name, "writer") for name in WRITER_TASKS]
    wanted += [(name, "writer") for name in sorted(tasks)
               if name.startswith(WRITER_TASK_PREFIXES) and name not in WRITER_TASKS]
    wanted += [(name, "retired_writer") for name in RETIRED_WRITER_TASKS]
    items = []
    for name, role in wanted:
        task = tasks.get(name)
        if task is None:
            items.append({"name": name, "exists": False, "state": "missing", "query_failed": False,
                          "error": "", "raw": "", "role": role, "processes": [],
                          "process_check": "complete" if process_check else "unavailable"})
            continue
        items.append({
            "name": name,
            "exists": True,
            "state": _normalize_state(task.get("state")),
            "query_failed": False,
            "error": "",
            "raw": "",
            "role": role,
            "processes": _writer_processes(task, snapshot) if process_check else [],
            "process_check": ("complete" if running_known else "partial") if process_check else "unavailable",
        })
    return items


def _legacy_writers(runner: RunFn) -> list[dict[str, Any]]:
    """schtasks-only fallback: no process table, so the process tree is unverified."""

    items = []
    for name in WRITER_TASKS:
        items.append({**_query_task(name, runner), "role": "writer", "processes": [], "process_check": "unavailable"})
    dated, error = _list_prefixed_tasks(runner, WRITER_TASK_PREFIXES)
    if error:
        for prefix in WRITER_TASK_PREFIXES:
            items.append({"name": prefix + " *", "exists": True, "state": "unknown", "query_failed": True,
                          "error": error, "raw": error, "role": "writer", "processes": [],
                          "process_check": "unavailable"})
    for name in dated:
        if name not in WRITER_TASKS:
            items.append({**_query_task(name, runner), "role": "writer", "processes": [], "process_check": "unavailable"})
    for name in RETIRED_WRITER_TASKS:
        items.append({**_query_task(name, runner), "role": "retired_writer", "processes": [],
                      "process_check": "unavailable"})
    return items


def _kill_process_tree(pid: int) -> dict[str, Any]:
    """Real killer: ``taskkill /F /T`` ends the process and all its descendants."""

    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(int(pid))],
            capture_output=True,
            timeout=KILL_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"pid": int(pid), "ok": False, "error": type(exc).__name__}
    detail = (_console_text(completed.stderr) or _console_text(completed.stdout)).strip()
    return {"pid": int(pid), "ok": completed.returncode == 0, "error": "" if completed.returncode == 0 else detail[:300]}


def _plan(item: dict[str, Any]) -> str:
    state = item.get("state")
    alive = bool(item.get("processes"))
    if not item["exists"]:
        return "missing"
    if item.get("query_failed"):
        return "query_failed"
    if _is_disabled_state(state):
        return "already_disabled_kill_orphans" if alive else "already_disabled"
    if _is_running_state(state) or _is_queued_state(state):
        return "end_then_disable"
    if _is_stopped_state(state):
        return "kill_orphans_then_disable" if alive else "disable"
    return "needs_confirmation"


class _Ops:
    """The side-effect surface of one pause/resume run (injectable for tests)."""

    def __init__(self, runner: RunFn, inventory: InventoryFn | None, killer: KillFn | None) -> None:
        self.runner = runner
        self.inventory = inventory
        self.killer = killer or _kill_process_tree
        self.killed: list[dict[str, Any]] = []

    def find(self, name: str) -> dict[str, Any] | None:
        snapshot = self.inventory() if self.inventory else None
        if snapshot is None:
            return None
        return next((item for item in _inventory_writers(snapshot) if item["name"] == name), None)

    def kill_top_level(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        results = []
        for proc in item.get("processes") or []:
            if proc.get("top_level"):
                result = self.killer(int(proc["pid"]))
                result["name"] = item["name"]
                results.append(result)
                self.killed.append(result)
        return results

    def confirm_stopped(self, name: str) -> dict[str, str]:
        """Prove the stop: scheduler not running *and* no matched process left."""

        if self.inventory is None:
            after = _query_task(name, self.runner)
            if after.get("query_failed"):
                return {"name": name, "stop": "waiting", "detail": str(after.get("error") or "scheduler_query_failed")}
            if _is_running_state(after.get("state")):
                return {"name": name, "stop": "waiting", "detail": "task_still_running"}
            if _is_stopped_state(after.get("state")):
                return {"name": name, "stop": "stopped", "detail": "process_tree_not_verified"}
            return {"name": name, "stop": "waiting", "detail": "stop_not_proven"}
        detail = "stop_not_proven"
        for attempt in range(STOP_POLLS):
            try:
                item = self.find(name)
            except (OSError, ValueError, TypeError) as exc:
                return {"name": name, "stop": "waiting", "detail": f"process_inventory_failed:{exc}"[:300]}
            if item is None or not item["exists"]:
                return {"name": name, "stop": "waiting", "detail": "task_disappeared"}
            if item["processes"]:
                self.kill_top_level(item)
                detail = "processes_still_alive"
            elif _is_running_state(item["state"]) or _is_queued_state(item["state"]):
                detail = "task_still_running"
            elif _is_stopped_state(item["state"]):
                return {"name": name, "stop": "stopped", "detail": "process_tree_verified"}
            else:
                detail = "stop_not_proven"
            if attempt + 1 < STOP_POLLS:
                _sleep(STOP_POLL_SECONDS)
        return {"name": name, "stop": "waiting", "detail": detail}


def _load_pause_record(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
    runner: RunFn | None = None,
    inventory: InventoryFn | None = None,
    killer: KillFn | None = None,
) -> dict[str, Any]:
    """Stop and disable writer tasks.  Missing tasks are noted, not invented.

    With no injected ``runner`` the real scheduler is read through one
    language-independent PowerShell inventory.  An injected ``runner`` without an
    ``inventory`` keeps the schtasks-only path (process tree unverified).
    """

    root = Path(project_root)
    run = runner or subprocess.run
    if inventory is None and runner is None:
        inventory = read_scheduler_inventory
    ops = _Ops(run, inventory, killer)
    state_source = "schtasks_fallback"
    inventory_error = ""
    queried: list[dict[str, Any]] = []
    if inventory is not None:
        try:
            queried = _inventory_writers(inventory())
            state_source = "get_scheduledtask"
        except (OSError, ValueError, TypeError) as exc:
            inventory_error = str(exc)[:300]
            ops.inventory = None
    if state_source != "get_scheduledtask":
        queried = _legacy_writers(run)
    path = _state_path(root)
    previous = _load_pause_record(path)
    paused: list[str] = [
        str(item)
        for item in (previous.get("paused_tasks") or [])
        if str(item).strip() and not previous.get("corrupt")
    ]
    skipped: list[str] = []
    failed: list[str] = []
    actions: list[dict[str, Any]] = []
    writer_stops: list[dict[str, str]] = []
    preview: list[dict[str, Any]] = []
    for item in queried:
        planned = _plan(item)
        item["planned_action"] = planned
        preview.append({
            "name": item["name"], "state": item.get("state"), "role": item.get("role"),
            "planned_action": planned,
            "stop_confirmation_required": planned == "end_then_disable",
            "processes": item.get("processes") or [],
            "process_check": item.get("process_check"),
            "error": item.get("error", ""),
        })
        if not apply and planned in {"query_failed", "needs_confirmation"}:
            failed.append(str(item["name"]))
    if apply:
        for item in queried:
            name = str(item["name"])
            planned = item["planned_action"]
            if planned == "missing":
                skipped.append(name)
                continue
            if planned in {"query_failed", "needs_confirmation"}:
                failed.append(name)
                detail = str(item.get("error") or "scheduler_query_failed") if planned == "query_failed" else "unknown_task_state"
                writer_stops.append({"name": name, "stop": "failed", "detail": detail})
                continue
            if planned.startswith("already_disabled"):
                skipped.append(name)
                if planned == "already_disabled_kill_orphans":
                    actions.extend({"action": "kill_tree", **r} for r in ops.kill_top_level(item))
                    stop = ops.confirm_stopped(name)
                    writer_stops.append(stop)
                    if stop["stop"] != "stopped":
                        failed.append(name)
                continue
            if planned == "end_then_disable":
                ended = _end_task(name, run)
                actions.append(ended)
                if not ended["ok"]:
                    failed.append(name)
                    writer_stops.append({"name": name, "stop": "failed", "detail": ended.get("error") or ""})
                    continue
                stop = ops.confirm_stopped(name)
                writer_stops.append(stop)
                if stop["stop"] != "stopped":
                    failed.append(name)
            elif planned == "kill_orphans_then_disable":
                actions.extend({"action": "kill_tree", **r} for r in ops.kill_top_level(item))
                stop = ops.confirm_stopped(name)
                writer_stops.append(stop)
                if stop["stop"] != "stopped":
                    failed.append(name)
            else:
                writer_stops.append({"name": name, "stop": "not_running_schedule_disabled"})
            result = _set_task(name, False, run)
            actions.append(result)
            if result["ok"]:
                if name not in paused:
                    paused.append(name)
            else:
                failed.append(name)
    failed = list(dict.fromkeys(failed))
    if apply:
        atomic_write_json(
            path,
            {
                "schema": "desk_control_v1",
                "paused_at": str(previous.get("paused_at") or now_text()),
                "updated_at": now_text(),
                "paused_tasks": paused,
                "failed_tasks": failed,
                "pending_stops": [row for row in writer_stops if row.get("stop") not in {"stopped", "not_running_schedule_disabled"}],
                "state_source": state_source,
                "disclaimer": DISCLAIMER,
            },
        )
    process_check = state_source == "get_scheduledtask" and all(
        item.get("process_check") == "complete" for item in queried if item["exists"]
    )
    return {
        "ok": not failed,
        "command": "pause",
        "apply": bool(apply),
        "disclaimer": DISCLAIMER,
        "state_source": state_source,
        "inventory_error": inventory_error,
        "writers": queried,
        "paused_tasks": paused,
        "failed_tasks": failed,
        "writer_stops": writer_stops,
        "skipped_missing_or_already_disabled": skipped,
        "actions": actions,
        "killed_processes": ops.killed,
        "preview": preview,
        "writer_process_tree_checked": process_check,
        "runtime_quiescence_proven": False,
        "readers_left_on": True,
        "reader_tasks": list(READER_TASKS),
        "note": (
            "管理清单里的写手：收盘链、两档 AI、盘中刷新、快照拉取、每日复盘、周宽池、AI Observation（及已退役的旧数据任务）。"
            "预览只读：读任务状态和进程表，不动任何任务。实际暂停：结束正在运行的任务，杀掉它留下的整棵进程树"
            "（按脚本路径认领孤儿进程），核实已停，再禁用下次调度；没核实到停止就报等待或失败。"
            "备份、健康行、完整性检查、早报继续运行。不证明手工启动的程序或整机已静默。暂停期间缺的数据不补。"
        ),
    }


def resume_writers(
    project_root: str | Path,
    *,
    apply: bool = True,
    runner: RunFn | None = None,
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
        "preview": [{"name": name, "planned_action": "enable_recorded_task"} for name in names],
        "failed_tasks": failed,
        "actions": actions,
        "note": "只恢复上次 pause 停掉的写手。旧的已禁用数据任务不会被重新打开。失败时保留暂停记录。",
    }


def read_pause_state(project_root: str | Path) -> dict[str, Any]:
    """Read-only view of ``state/desk_control.json`` for the health line and 8000 page."""

    path = _state_path(Path(project_root))
    if not path.is_file():
        return {"paused": False, "corrupt": False, "paused_at": "", "paused_tasks": [], "failed_tasks": []}
    stored = _load_pause_record(path)
    if stored.get("corrupt"):
        return {"paused": False, "corrupt": True, "paused_at": "", "paused_tasks": [], "failed_tasks": []}
    paused = [str(item) for item in (stored.get("paused_tasks") or []) if str(item).strip()]
    failed = [str(item) for item in (stored.get("failed_tasks") or stored.get("resume_failed_tasks") or [])
              if str(item).strip()]
    return {
        "paused": bool(paused),
        "corrupt": False,
        "paused_at": str(stored.get("paused_at") or ""),
        "paused_tasks": paused,
        "failed_tasks": failed,
    }


def pause_cell(state: dict[str, Any]) -> tuple[str, str]:
    """(cell, explanation) for a pause record; ("", "") when nothing is paused."""

    if state.get("corrupt"):
        return "暂停记录 ✗损坏", "state/desk_control.json 读不出：qd resume 不会打开任何写手，需人工核对计划任务"
    paused = list(state.get("paused_tasks") or [])
    failed = list(state.get("failed_tasks") or [])
    if paused:
        explain = (f"qd pause 自 {state.get('paused_at') or '未知时刻'} 起暂停 {len(paused)} 个写手："
                   + "、".join(paused) + "；暂停期间缺的数据就是缺，恢复用 qd resume")
        cell = f"暂停中 {len(paused)}个写手"
        if failed:
            cell += f" ✗{len(failed)}个未停"
            explain += "；未确认停止：" + "、".join(failed)
        return cell, explain
    if failed:
        return f"暂停 ✗{len(failed)}个未停", "上次 qd pause 没停住：" + "、".join(failed)
    return "", ""
