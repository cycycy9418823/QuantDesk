from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from quantdesk.desk_control import WRITER_TASKS, pause_writers, resume_writers
from quantdesk.pipeline_terminal import classify_pipeline_status


class _FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.states = {
            "QuantDesk Data Close Pipeline": "Ready",
            "QuantDesk AI Trader Evening": "Ready",
        }

    def __call__(self, argv, **kwargs):
        self.commands.append(list(argv))
        if argv[1] == "/Query":
            name = argv[3]
            if name not in self.states:
                return SimpleNamespace(returncode=1, stdout="", stderr="not found")
            return SimpleNamespace(
                returncode=0,
                stdout=f"TaskName: {name}\nStatus: {self.states[name]}\n",
                stderr="",
            )
        if argv[1] == "/End":
            name = argv[3]
            if self.states.get(name) == "Running":
                self.states[name] = "Ready"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1] == "/Change":
            name = argv[3]
            if "/DISABLE" in argv:
                self.states[name] = "Disabled"
            if "/ENABLE" in argv:
                self.states[name] = "Ready"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)


def test_pause_disables_only_ready_writers_and_resume_restores_them(tmp_path: Path) -> None:
    runner = _FakeRunner()
    paused = pause_writers(tmp_path, apply=True, runner=runner)
    assert paused["paused_tasks"] == [
        "QuantDesk Data Close Pipeline",
        "QuantDesk AI Trader Evening",
    ]
    assert "QuantDesk Snapshot Collector" in paused["skipped_missing_or_already_disabled"]
    assert runner.states["QuantDesk Data Close Pipeline"] == "Disabled"
    state = tmp_path / "state" / "desk_control.json"
    assert state.is_file()

    resumed = resume_writers(tmp_path, apply=True, runner=runner)
    assert set(resumed["resumed_tasks"]) == {
        "QuantDesk Data Close Pipeline",
        "QuantDesk AI Trader Evening",
    }
    assert runner.states["QuantDesk Data Close Pipeline"] == "Ready"
    assert state.exists() is False


def test_pause_without_apply_does_not_change_tasks(tmp_path: Path) -> None:
    runner = _FakeRunner()
    payload = pause_writers(tmp_path, apply=False, runner=runner)
    assert payload["paused_tasks"] == []
    assert runner.states["QuantDesk Data Close Pipeline"] == "Ready"
    assert not (tmp_path / "state" / "desk_control.json").exists()


def test_pipeline_health_alert_push_lock_is_skip_not_failure() -> None:
    result = classify_pipeline_status(
        {
            "universe": {"status": "completed"},
            "market_refresh": {"status": "completed"},
            "research_chain": {"status": "completed"},
            "reconcile": {"status": "completed"},
            "health_alert": {
                "status": "failed",
                "reason_codes": ["stage_exit_code:health_alert:4"],
                "payload": {
                    "stdout_json": {
                        "status": "blocked",
                        "reason_code": "data_health_alert_push_not_explicitly_enabled",
                    }
                },
            },
        }
    )
    assert result["status"] == "completed_with_skips"
    assert result["failed_stages"] == []
    assert result["skipped_stages"][0]["stage"] == "health_alert"


def test_pipeline_all_executed_and_hard_failure() -> None:
    ok = classify_pipeline_status(
        {name: {"status": "completed"} for name in ("universe", "market_refresh", "research_chain", "reconcile", "health_alert")}
    )
    assert ok["status"] == "completed_all_executed"
    bad = classify_pipeline_status(
        {
            "universe": {"status": "completed"},
            "market_refresh": {"status": "failed"},
        }
    )
    assert bad["status"] == "failed"
    assert "market_refresh" in bad["failed_stages"]


def test_writer_task_names_match_charter() -> None:
    assert "QuantDesk Data Close Pipeline" in WRITER_TASKS
    assert "QuantDesk AI Trader Evening" in WRITER_TASKS


def test_pause_ends_running_writer_before_disable(tmp_path: Path) -> None:
    runner = _FakeRunner()
    runner.states["QuantDesk Data Close Pipeline"] = "Running"
    paused = pause_writers(tmp_path, apply=True, runner=runner)
    assert ["schtasks", "/End", "/TN", "QuantDesk Data Close Pipeline"] in runner.commands
    assert runner.states["QuantDesk Data Close Pipeline"] == "Disabled"
    assert any(row.get("stop") == "stopped" for row in paused["writer_stops"])


def test_second_pause_keeps_previous_list_and_resume_does_not_enable_all(tmp_path: Path) -> None:
    runner = _FakeRunner()
    runner.states["QuantDesk AI Trader Evening"] = "Disabled"
    first = pause_writers(tmp_path, apply=True, runner=runner)
    assert first["paused_tasks"] == ["QuantDesk Data Close Pipeline"]
    second = pause_writers(tmp_path, apply=True, runner=runner)
    assert second["paused_tasks"] == ["QuantDesk Data Close Pipeline"]
    resumed = resume_writers(tmp_path, apply=True, runner=runner)
    assert resumed["resumed_tasks"] == ["QuantDesk Data Close Pipeline"]
    assert runner.states["QuantDesk AI Trader Evening"] == "Disabled"


def test_resume_without_pause_record_does_not_enable_writers(tmp_path: Path) -> None:
    runner = _FakeRunner()
    runner.states["QuantDesk AI Trader Evening"] = "Disabled"
    payload = resume_writers(tmp_path, apply=True, runner=runner)
    assert payload["ok"] is False
    assert payload["reason"] == "no_pause_record"
    assert runner.states["QuantDesk Data Close Pipeline"] == "Ready"
    assert runner.states["QuantDesk AI Trader Evening"] == "Disabled"


def test_empty_pipeline_is_not_completed() -> None:
    empty = classify_pipeline_status({})
    assert empty["status"] == "failed"
    timeout = classify_pipeline_status({"market_refresh": {"status": "timeout"}})
    assert timeout["status"] == "failed"
    assert "market_refresh" in timeout["failed_stages"]
