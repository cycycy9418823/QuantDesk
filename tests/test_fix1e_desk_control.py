"""Fix 1E: qd pause must read a Chinese Windows scheduler and stop whole writer trees.

Every scheduler / process call here is a fake. Nothing touches real tasks.
"""
from __future__ import annotations

import base64
import json
import os
from types import SimpleNamespace

import pytest

from quantdesk import desk_control as control

ROOT = r"E:\codex\quant_new"
PY = ROOT + r"\.venv\Scripts\python.exe"
PYW = ROOT + r"\.venv\Scripts\pythonw.exe"
LAUNCHER = ROOT + r"\scripts\run_scheduled_task.py"
PIPELINE_SCRIPT = ROOT + r"\scripts\run_data_close_pipeline.py"


@pytest.fixture(autouse=True)
def no_real_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("real subprocess forbidden in this test")

    monkeypatch.setattr(control.subprocess, "run", forbidden)
    monkeypatch.setattr(control, "_sleep", lambda seconds: None)


def launcher_action(task: str, policy: str, inner_exe: str, inner_args: str) -> dict:
    encoded = base64.b64encode(json.dumps({
        "arguments": inner_args, "working_directory": ROOT, "executable": inner_exe,
    }).encode("utf-8")).decode("ascii")
    return {"execute": PYW, "arguments": f'-B "{LAUNCHER}" --task-name "{task}" --policy {policy} --action-base64 {encoded}'}


def machine_tasks() -> list[dict]:
    """The 18 QuantDesk tasks with the states observed on the owner's machine 2026-10-11."""

    def task(name, state, action):
        return {"name": name, "path": "\\", "state": state, "actions": [action]}

    cmd = r"C:\Windows\System32\cmd.exe"
    return [
        task("QuantDesk A0 Snapshot Collector", "Disabled", {"execute": PY, "arguments": f'-B "{ROOT}\\scripts\\collect_market_snapshots.py" --enable-network'}),
        task("QuantDesk AI Observation 20261008", "Ready", launcher_action("QuantDesk AI Observation 20261008", "trading_day", PY, f'-B "{ROOT}\\scripts\\run_daytime_observation.py" --enable-observation')),
        task("QuantDesk AI Trader Evening", "Ready", launcher_action("QuantDesk AI Trader Evening", "trading_day", cmd, f'/d /s /c ""{ROOT}\\scripts\\run_ai_trader_evening.cmd""')),
        task("QuantDesk AI Trader Evening (GLM)", "Ready", launcher_action("QuantDesk AI Trader Evening (GLM)", "trading_day", cmd, f'/d /s /c ""{ROOT}\\scripts\\run_ai_trader_evening_glm.cmd""')),
        task("QuantDesk Backup Push", "Ready", launcher_action("QuantDesk Backup Push", "daily", PY, f'-B "{ROOT}\\scripts\\ops\\push_backup_to_student.py" --json true')),
        task("QuantDesk Daily Data Chain", "Disabled", {"execute": "powershell.exe", "arguments": f'-File "{ROOT}\\scripts\\run_daily_data_chain_task.ps1"'}),
        task("QuantDesk Daily Data Refresh", "Disabled", {"execute": PY, "arguments": f'-B "{ROOT}\\scripts\\run_daily_data_refresh.py"'}),
        task("QuantDesk Daily Integrity Check", "Ready", {"execute": cmd, "arguments": f'/d /s /c ""{ROOT}\\scripts\\run_daily_integrity_check.cmd""'}),
        task("QuantDesk Daily Review Routine", "Ready", launcher_action("QuantDesk Daily Review Routine", "trading_day", cmd, f'/d /s /c ""{ROOT}\\scripts\\run_daily_review_routine.cmd" --with-guardrails"')),
        task("QuantDesk Data Close Pipeline", "Ready", launcher_action("QuantDesk Data Close Pipeline", "trading_day", PY, f'-B "{PIPELINE_SCRIPT}" --enable-network --trigger scheduled')),
        task("QuantDesk Full A Daily Reconciliation", "Disabled", {"execute": cmd, "arguments": f'/d /s /c ""{ROOT}\\scripts\\run_full_a_daily_reconciliation.cmd""'}),
        task("QuantDesk Health Line", "Ready", launcher_action("QuantDesk Health Line", "trading_day", PY, f'-B "{ROOT}\\scripts\\ops\\write_health_line.py" --json true')),
        task("QuantDesk Intraday Data Refresh", "Ready", launcher_action("QuantDesk Intraday Data Refresh", "trading_day", PY, f'-B "{ROOT}\\scripts\\run_intraday_data_refresh.py" --enable-network --no-alerts')),
        task("QuantDesk Morning Brief", "Ready", launcher_action("QuantDesk Morning Brief", "trading_day", cmd, f'/d /s /c ""{ROOT}\\scripts\\run_morning_brief.cmd" --enable-network --push"')),
        task("QuantDesk Snapshot Pull", "Ready", launcher_action("QuantDesk Snapshot Pull", "trading_day", PY, f'-B "{ROOT}\\scripts\\ops\\pull_snapshots_from_student.py" --json true')),
        task("QuantDesk Universe Baseline Refresh", "Disabled", {"execute": PY, "arguments": f'-B "{ROOT}\\scripts\\refresh_universe_baseline.py"'}),
        task("QuantDesk Weekly Daily Wide Pool", "Ready", {"execute": cmd, "arguments": f'/d /s /c ""{ROOT}\\scripts\\run_weekly_wide_pool_maintenance.cmd" --daily-only --enable-network --apply"'}),
        task("QuantDesk Weekly Minute Wide Pool", "Disabled", {"execute": cmd, "arguments": f'/d /s /c ""{ROOT}\\scripts\\run_weekly_minute_wide_pool.cmd""'}),
    ]


class World:
    """Fake Task Scheduler + process table. /End kills only the engine process."""

    def __init__(self, *, running=None, processes=None, survivors=()):
        self.tasks = {t["name"]: t for t in machine_tasks()}
        self.running = dict(running or {})        # task name -> engine pid
        self.processes = {p["pid"]: dict(p) for p in (processes or [])}
        self.survivors = set(survivors)           # pids that ignore taskkill
        self.commands: list[list[str]] = []
        self.killed: list[int] = []
        self.inventory_calls = 0

    def snapshot(self) -> dict:
        self.inventory_calls += 1
        for name in self.tasks:
            if name in self.running and self.tasks[name]["state"] != "Disabled":
                self.tasks[name]["state"] = "Running"
        return {
            "tasks": [json.loads(json.dumps(t)) for t in self.tasks.values()],
            "running": [{"path": "\\" + n, "engine_pid": pid} for n, pid in self.running.items()],
            "processes": [dict(p) for p in self.processes.values()],
            "shell_pid": 999_001,
        }

    def runner(self, argv, **kwargs):
        self.commands.append(list(argv))
        assert argv[0] == "schtasks" and argv[1] in {"/End", "/Change"}, argv
        name = argv[3]
        if argv[1] == "/End":
            pid = self.running.pop(name, None)
            if pid is not None:
                self.processes.pop(pid, None)   # the scheduler ends only its own process
            if self.tasks[name]["state"] == "Running":
                self.tasks[name]["state"] = "Ready"
        else:
            self.tasks[name]["state"] = "Disabled" if "/DISABLE" in argv else "Ready"
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def killer(self, pid: int) -> dict:
        self.killed.append(pid)
        if pid in self.survivors:
            return {"pid": pid, "ok": False, "error": "Access is denied."}
        doomed = {pid}
        changed = True
        while changed:
            changed = False
            for proc in self.processes.values():
                if proc["ppid"] in doomed and proc["pid"] not in doomed and proc["pid"] not in self.survivors:
                    doomed.add(proc["pid"])
                    changed = True
        for item in doomed:
            self.processes.pop(item, None)
        return {"pid": pid, "ok": True, "error": ""}

    def pause(self, root, apply):
        return control.pause_writers(root, apply=apply, runner=self.runner,
                                     inventory=self.snapshot, killer=self.killer)


def proc(pid, ppid, name, cmd, created="2026-10-12T07:40:00.000Z"):
    return {"pid": pid, "ppid": ppid, "name": name, "created": created, "command_line": cmd}


def pipeline_tree():
    """Scheduler engine -> venv python -> pipeline script -> two workers."""

    task = next(t for t in machine_tasks() if t["name"] == "QuantDesk Data Close Pipeline")
    launcher_cmd = f'"{PYW}" {task["actions"][0]["arguments"]}'
    return [
        proc(4100, 600, "pythonw.exe", launcher_cmd, "2026-10-12T07:40:00.000Z"),
        proc(4200, 4100, "python.exe", f'"{PY}" -B "{PIPELINE_SCRIPT}" --enable-network', "2026-10-12T07:40:01.000Z"),
        proc(4300, 4200, "python.exe", f'"{PY}" -m quantdesk.worker --stage market_refresh', "2026-10-12T07:40:05.000Z"),
        proc(4400, 4300, "conhost.exe", "conhost.exe 0x4", "2026-10-12T07:40:05.500Z"),
    ]


def bystanders():
    return [
        proc(5000, 600, "notepad.exe", f'notepad.exe "{PIPELINE_SCRIPT}"'),
        proc(5100, 600, "python.exe", f'python.exe -B "E:\\codex\\tmp\\w1e\\scripts\\run_data_close_pipeline.py"'),
        proc(5200, 600, "python.exe", f'"{PY}" -B "{ROOT}\\scripts\\ops\\push_backup_to_student.py" --json true'),
        proc(os.getpid(), 600, "python.exe", f'"{PY}" "{PIPELINE_SCRIPT}" pretend-self'),
    ]


# ---------------------------------------------------------------- schtasks fallback (zh-CN)
ZH_LIST = ("\r\n文件夹: \\\r\n主机名:       LAPTOP\r\n任务名:       \\{name}\r\n"
           "下次运行时间: 2026-10-12 3:40:00 PM\r\n模式:         {state}\r\n登录状态:     交互方式/后台方式\r\n")


def zh_runner(states: dict[str, str], commands: list):
    def run(argv, **kwargs):
        commands.append(list(argv))
        if argv[1] == "/Query" and "/TN" in argv:
            name = argv[3]
            if name not in states:
                return SimpleNamespace(returncode=1, stdout=b"",
                                       stderr="错误: 系统找不到指定的文件。\r\n".encode("gbk"))
            return SimpleNamespace(returncode=0, stdout=ZH_LIST.format(name=name, state=states[name]).encode("gbk"), stderr=b"")
        if argv[1] == "/Query":
            rows = "".join(f'"\\{n}","N/A","{s}"\r\n' * 2 for n, s in states.items())
            return SimpleNamespace(returncode=0, stdout=rows.encode("gbk"), stderr=b"")
        states[argv[3]] = "已禁用" if "/DISABLE" in argv else "就绪"
        return SimpleNamespace(returncode=0, stdout="成功: 已更改计划任务。\r\n".encode("gbk"), stderr=b"")
    return run


def test_zh_cn_schtasks_gbk_mode_ready_is_understood(tmp_path):
    """Real zh-CN output is GBK and says "模式: 就绪"; before 1E every state was unknown."""

    states = {"QuantDesk Data Close Pipeline": "就绪", "QuantDesk AI Observation 20261008": "就绪",
              "QuantDesk A0 Snapshot Collector": "已禁用"}
    commands: list = []
    preview = control.pause_writers(tmp_path, apply=False, runner=zh_runner(states, commands))
    plans = {row["name"]: row["planned_action"] for row in preview["preview"]}
    assert preview["state_source"] == "schtasks_fallback"
    assert plans["QuantDesk Data Close Pipeline"] == "disable"
    assert plans["QuantDesk AI Observation 20261008"] == "disable"
    assert plans["QuantDesk A0 Snapshot Collector"] == "already_disabled"
    assert plans["QuantDesk Intraday Data Refresh"] == "missing"   # GBK "找不到" is missing, not a failure
    assert preview["ok"] is True and preview["failed_tasks"] == []
    applied = control.pause_writers(tmp_path, apply=True, runner=zh_runner(states, commands))
    assert applied["ok"] is True
    assert applied["paused_tasks"] == ["QuantDesk Data Close Pipeline", "QuantDesk AI Observation 20261008"]
    assert states["QuantDesk Data Close Pipeline"] == "已禁用"


def test_logon_status_line_is_not_the_task_state(tmp_path):
    text = ZH_LIST.replace("模式:         {state}\r\n", "")

    def run(argv, **kwargs):
        if "/TN" in argv:
            return SimpleNamespace(returncode=0, stdout=text.format(name=argv[3], state="").encode("gbk"), stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    result = control.pause_writers(tmp_path, apply=False, runner=run)
    first = next(row for row in result["preview"] if row["name"] == "QuantDesk Data Close Pipeline")
    assert first["state"] == "unknown" and first["planned_action"] == "needs_confirmation"
    assert result["ok"] is False


def test_inventory_covers_every_writer_and_leaves_readers_alone(tmp_path):
    world = World(processes=bystanders())
    result = world.pause(tmp_path, apply=False)
    plans = {row["name"]: row["planned_action"] for row in result["preview"]}
    assert result["state_source"] == "get_scheduledtask" and result["ok"] is True
    for name in ("QuantDesk Data Close Pipeline", "QuantDesk AI Trader Evening", "QuantDesk AI Trader Evening (GLM)",
                 "QuantDesk Intraday Data Refresh", "QuantDesk Snapshot Pull", "QuantDesk Daily Review Routine",
                 "QuantDesk Weekly Daily Wide Pool", "QuantDesk AI Observation 20261008"):
        assert plans[name] == "disable", name
    assert plans["QuantDesk A0 Snapshot Collector"] == "already_disabled"
    assert plans["QuantDesk Daily Data Refresh"] == "already_disabled"
    assert not set(control.READER_TASKS) & set(plans)
    assert world.commands == [] and world.killed == []
    assert result["writer_process_tree_checked"] is True
    assert not (tmp_path / "state" / "desk_control.json").exists()


def test_running_pipeline_whole_tree_is_killed_then_disabled(tmp_path):
    world = World(running={"QuantDesk Data Close Pipeline": 4100}, processes=pipeline_tree() + bystanders())
    preview = world.pause(tmp_path, apply=False)
    row = next(r for r in preview["preview"] if r["name"] == "QuantDesk Data Close Pipeline")
    assert row["planned_action"] == "end_then_disable"
    assert sorted(p["pid"] for p in row["processes"]) == [4100, 4200, 4300, 4400]
    assert world.killed == [] and world.commands == []

    result = world.pause(tmp_path, apply=True)
    assert result["ok"] is True, result["writer_stops"]
    assert world.commands[0] == ["schtasks", "/End", "/TN", "QuantDesk Data Close Pipeline"]
    assert world.commands[1] == ["schtasks", "/Change", "/TN", "QuantDesk Data Close Pipeline", "/DISABLE"]
    assert world.killed == [4200]          # orphaned script process after /End, with its children
    assert not {4100, 4200, 4300, 4400} & set(world.processes)
    assert {5000, 5100, 5200, os.getpid()} <= set(world.processes)
    stop = next(s for s in result["writer_stops"] if s["name"] == "QuantDesk Data Close Pipeline")
    assert stop == {"name": "QuantDesk Data Close Pipeline", "stop": "stopped", "detail": "process_tree_verified"}
    assert not any(cmd[3] in control.READER_TASKS for cmd in world.commands)
    stored = json.loads((tmp_path / "state" / "desk_control.json").read_text(encoding="utf-8"))
    assert "QuantDesk Data Close Pipeline" in stored["paused_tasks"] and stored["pending_stops"] == []


def test_orphan_writer_from_killed_launcher_is_stopped(tmp_path):
    """2026-10-09 15:46: the launcher died on battery, its child kept writing until 15:56."""

    orphan = proc(6100, 7777, "python.exe", f'"{PY}" -B "{ROOT}\\scripts\\run_intraday_data_refresh.py" --enable-network --no-alerts')
    world = World(processes=[orphan])
    preview = world.pause(tmp_path, apply=False)
    row = next(r for r in preview["preview"] if r["name"] == "QuantDesk Intraday Data Refresh")
    assert row["state"] == "Ready" and row["planned_action"] == "kill_orphans_then_disable"
    result = world.pause(tmp_path, apply=True)
    assert world.killed == [6100] and 6100 not in world.processes
    assert ["schtasks", "/End", "/TN", "QuantDesk Intraday Data Refresh"] not in world.commands
    assert "QuantDesk Intraday Data Refresh" in result["paused_tasks"] and result["ok"] is True


def test_unkillable_writer_is_waiting_not_stopped(tmp_path):
    world = World(running={"QuantDesk Data Close Pipeline": 4100}, processes=pipeline_tree(), survivors={4200})
    result = world.pause(tmp_path, apply=True)
    stop = next(s for s in result["writer_stops"] if s["name"] == "QuantDesk Data Close Pipeline")
    assert stop["stop"] == "waiting" and stop["detail"] == "processes_still_alive"
    assert result["ok"] is False and "QuantDesk Data Close Pipeline" in result["failed_tasks"]
    stored = json.loads((tmp_path / "state" / "desk_control.json").read_text(encoding="utf-8"))
    assert [row["name"] for row in stored["pending_stops"]] == ["QuantDesk Data Close Pipeline"]
    assert control.pause_cell(control.read_pause_state(tmp_path))[0].endswith("✗1个未停")


def test_similar_task_names_do_not_claim_each_others_processes(tmp_path):
    glm = next(t for t in machine_tasks() if t["name"] == "QuantDesk AI Trader Evening (GLM)")
    procs = [
        proc(4500, 600, "pythonw.exe", f'"{PYW}" {glm["actions"][0]["arguments"]}'),
        proc(4510, 4500, "cmd.exe", f'cmd.exe /d /s /c ""{ROOT}\\scripts\\run_ai_trader_evening_glm.cmd""', "2026-10-12T12:40:01.000Z"),
        proc(4520, 4510, "python.exe", f'"{PY}" -m ai_trader.run_evening --network', "2026-10-12T12:40:02.000Z"),
    ]
    world = World(running={"QuantDesk AI Trader Evening (GLM)": 4500}, processes=procs)
    result = world.pause(tmp_path, apply=False)
    rows = {r["name"]: r for r in result["preview"]}
    assert rows["QuantDesk AI Trader Evening"]["processes"] == []
    assert rows["QuantDesk AI Trader Evening"]["planned_action"] == "disable"
    assert sorted(p["pid"] for p in rows["QuantDesk AI Trader Evening (GLM)"]["processes"]) == [4500, 4510, 4520]


def test_recycled_parent_pid_is_not_a_descendant(tmp_path):
    procs = pipeline_tree() + [proc(4999, 4200, "python.exe", "python.exe unrelated.py", "2026-10-12T06:00:00.000Z")]
    world = World(running={"QuantDesk Data Close Pipeline": 4100}, processes=procs)
    row = next(r for r in world.pause(tmp_path, apply=False)["preview"] if r["name"] == "QuantDesk Data Close Pipeline")
    assert 4999 not in {p["pid"] for p in row["processes"]}


def test_running_reader_is_never_touched(tmp_path):
    backup = next(t for t in machine_tasks() if t["name"] == "QuantDesk Backup Push")
    procs = [proc(5300, 600, "pythonw.exe", f'"{PYW}" {backup["actions"][0]["arguments"]}')]
    world = World(running={"QuantDesk Backup Push": 5300}, processes=procs)
    result = world.pause(tmp_path, apply=True)
    assert result["ok"] is True
    assert world.killed == [] and 5300 in world.processes
    assert not any(cmd[3] == "QuantDesk Backup Push" for cmd in world.commands)
    assert world.tasks["QuantDesk Backup Push"]["state"] == "Running"


def test_inventory_failure_falls_back_to_schtasks_and_says_so(tmp_path):
    def broken():
        raise OSError("scheduler_inventory_timeout")

    states = {"QuantDesk Data Close Pipeline": "就绪"}
    result = control.pause_writers(tmp_path, apply=False, runner=zh_runner(states, []), inventory=broken)
    assert result["state_source"] == "schtasks_fallback"
    assert "timeout" in result["inventory_error"]
    assert result["writer_process_tree_checked"] is False
    row = next(r for r in result["preview"] if r["name"] == "QuantDesk Data Close Pipeline")
    assert row["planned_action"] == "disable" and row["process_check"] == "unavailable"


def test_queued_writer_is_stopped_like_running(tmp_path):
    world = World()
    world.tasks["QuantDesk Snapshot Pull"]["state"] = "Queued"
    row = next(r for r in world.pause(tmp_path, apply=False)["preview"] if r["name"] == "QuantDesk Snapshot Pull")
    assert row["planned_action"] == "end_then_disable"


def test_re_enabled_retired_writer_is_paused_too(tmp_path):
    world = World()
    world.tasks["QuantDesk Daily Data Refresh"]["state"] = "Ready"
    result = world.pause(tmp_path, apply=True)
    assert "QuantDesk Daily Data Refresh" in result["paused_tasks"]
    resumed = control.resume_writers(tmp_path, apply=True, runner=world.runner)
    assert "QuantDesk Daily Data Refresh" in resumed["resumed_tasks"]
    assert world.tasks["QuantDesk Weekly Minute Wide Pool"]["state"] == "Disabled"   # never paused, never enabled


@pytest.mark.parametrize("value, expected", [("3", "Ready"), ("4", "Running"), ("1", "Disabled"), ("2", "Queued"),
                                             ("就绪", "Ready"), ("正在运行", "Running"), ("已禁用", "Disabled")])
def test_state_normalization_is_language_independent(value, expected):
    assert control._normalize_state(value) == expected


def test_pause_cell_wording(tmp_path):
    assert control.pause_cell(control.read_pause_state(tmp_path)) == ("", "")
    path = tmp_path / "state" / "desk_control.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"paused_at": "2026-10-12 09:00:00", "paused_tasks": ["A", "B"], "failed_tasks": []}), encoding="utf-8")
    cell, explain = control.pause_cell(control.read_pause_state(tmp_path))
    assert cell == "暂停中 2个写手" and "✗" not in cell
    assert "2026-10-12 09:00:00" in explain and "qd resume" in explain
    path.write_text("{not json", encoding="utf-8")
    cell, explain = control.pause_cell(control.read_pause_state(tmp_path))
    assert "✗" in cell and "不会打开任何写手" in explain


def test_real_inventory_reader_parses_bom_json_and_reports_failures(monkeypatch):
    payload = {"tasks": [{"name": "QuantDesk Data Close Pipeline", "path": "\\", "state": "Ready", "actions": []}],
               "running": [], "processes": [], "shell_pid": 1}
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"), stderr=b"")

    monkeypatch.setattr(control.subprocess, "run", fake_run)
    assert control.read_scheduler_inventory()["tasks"][0]["state"] == "Ready"
    assert seen["argv"][0] == "powershell.exe"
    monkeypatch.setattr(control.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"", stderr=b"denied"))
    with pytest.raises(OSError, match="scheduler_inventory_failed"):
        control.read_scheduler_inventory()


def test_inventory_script_is_read_only():
    script = control._INVENTORY_SCRIPT.lower()
    for verb in ("register-scheduledtask", "set-scheduledtask", "start-scheduledtask", "stop-scheduledtask",
                 "disable-scheduledtask", "enable-scheduledtask", "unregister-scheduledtask", "schtasks",
                 "stop-process", "taskkill", "invoke-webrequest", "invoke-restmethod"):
        assert verb not in script, verb
