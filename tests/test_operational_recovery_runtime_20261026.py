from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ensure_research_viewer as viewer
from scripts import launcher


def backup_module():
    path = Path(__file__).resolve().parents[1] / "scripts/ops/push_backup_to_student.py"
    spec = importlib.util.spec_from_file_location("ops_recovery_backup_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def identity(tmp_path, monkeypatch):
    root = tmp_path / "source"
    entry = root / "scripts/run_research_viewer.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# deterministic viewer fixture\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "server_source_build_id", lambda p: "fixture-build")
    caps = {"ok": True, "mode": "read_only_research", "read_only": True,
            "mutations_enabled": False, "selected_quote_refresh": False,
            "source_identity": {"data_root": str(root), "server_build_id": "fixture-build",
                                "entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest()}}
    return root, caps


def test_login_start_and_repeated_start_create_only_one_process(identity, tmp_path):
    root, caps = identity
    active = [False]
    launched = []
    def start(command, **kwargs):
        launched.append(command)
        active[0] = True
        return SimpleNamespace(pid=123, poll=lambda: None)
    get = lambda port: caps if active[0] else None
    first = viewer.ensure_research_viewer(root, tmp_path / "logs", start=start, capabilities=get, port_open=lambda port: False)
    second = viewer.ensure_research_viewer(root, tmp_path / "logs", start=start, capabilities=get, port_open=lambda port: True)
    assert first["status"] == "ready" and second["status"] == "reused" and len(launched) == 1
    assert "--enable-selected-quotes" not in launched[0] and "--enable-network" not in launched[0]
    assert "--data-root" in launched[0] and "--port" in launched[0]


def test_unknown_port_owner_is_preserved(identity, tmp_path):
    root, caps = identity
    caps["mutations_enabled"] = True
    called = []
    result = viewer.ensure_research_viewer(root, tmp_path / "logs", start=lambda *a, **k: called.append(a),
                                          capabilities=lambda port: caps, port_open=lambda port: True)
    assert result["ok"] is False and result["process_stopped"] is False and called == []


def test_viewer_refuses_to_write_logs_into_data_workspace(identity):
    root, caps = identity
    with pytest.raises(ValueError, match="outside_project"):
        viewer.ensure_research_viewer(root, root / "logs", capabilities=lambda port: caps)


def test_new_viewer_failure_leaves_a_bounded_external_receipt(identity, tmp_path):
    root, caps = identity
    clock = [0.0]
    result = viewer.ensure_research_viewer(
        root, tmp_path / "logs", capabilities=lambda port: None, port_open=lambda port: False,
        start=lambda *a, **kw: SimpleNamespace(pid=1, poll=lambda: 2),
        clock=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds), wait_seconds=1)
    assert result["status"] == "exited_before_ready" and result["exit_code"] == 2
    assert (Path(result["run_directory"]) / "result.json").is_file()


def test_transient_ssh_disconnect_retries_once_within_one_step_budget(monkeypatch):
    mod = backup_module()
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    calls = []
    def run(cmd, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(255, cmd, stderr="Connection reset by peer")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    result = mod._run(["ssh", "fixture"], run=run, timeout=120, retry_transient=True)
    assert result.returncode == 0 and len(calls) == 2
    assert calls[0]["timeout"] <= 60 and calls[1]["timeout"] <= 120
    assert all(call["capture_output"] and call["errors"] == "replace" for call in calls)


@pytest.mark.parametrize("message,category", [
    ("Host key verification failed", "host_verification_failed"),
    ("Permission denied (publickey)", "authentication_failed"),
])
def test_identity_failures_are_visible_and_never_retried(monkeypatch, message, category):
    mod = backup_module()
    calls = []
    def run(cmd, **kwargs):
        calls.append(1)
        raise subprocess.CalledProcessError(255, cmd, stderr=message)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        mod._run(["ssh", "fixture"], run=run, timeout=120, retry_transient=True)
    assert calls == [1] and mod.transport_failure(caught.value)["error_category"] == category


def test_errors_redact_credentials_before_receipt_output():
    mod = backup_module()
    text = mod.redact_transport_error("Authorization: Bearer abc123 token=secret-value password=hidden")
    assert "abc123" not in text and "secret-value" not in text and "hidden" not in text
    assert "[REDACTED]" in text


@pytest.mark.parametrize("enabled", [False, True])
def test_selected_quote_permission_is_explicit_and_part_of_identity(identity, tmp_path, enabled):
    root, caps = identity
    caps["selected_quote_refresh"] = enabled
    started = []
    def start(command, **kwargs):
        started.append(command)
        return SimpleNamespace(pid=123, poll=lambda: None)
    result = viewer.ensure_research_viewer(
        root, tmp_path / "logs", enable_selected_quotes=enabled,
        capabilities=lambda port: caps if started else None, port_open=lambda port: False, start=start)
    assert result["ok"] and result["selected_quote_refresh"] is enabled
    assert ("--enable-selected-quotes" in started[0]) is enabled
    assert viewer.viewer_identity_matches(caps, root, enable_selected_quotes=enabled)
    assert not viewer.viewer_identity_matches(caps, root, enable_selected_quotes=not enabled)


def test_concurrent_start_cannot_spawn_a_second_process(identity, tmp_path):
    root, caps = identity
    nested = []
    started = []
    def start(command, **kwargs):
        started.append(command)
        nested.append(viewer.ensure_research_viewer(
            root, tmp_path / "logs", start=lambda *a, **kw: pytest.fail("duplicate launch"),
            capabilities=lambda port: None, port_open=lambda port: False))
        return SimpleNamespace(pid=1, poll=lambda: None)
    result = viewer.ensure_research_viewer(
        root, tmp_path / "logs", start=start,
        capabilities=lambda port: caps if started else None, port_open=lambda port: False)
    assert result["ok"] and len(started) == 1
    assert nested[0]["status"] == "startup_in_progress" and not nested[0]["process_stopped"]


def test_spawn_failure_has_an_external_receipt(identity, tmp_path):
    root, _ = identity
    def start(*args, **kwargs):
        raise OSError("injected launch failure")
    result = viewer.ensure_research_viewer(root, tmp_path / "logs", start=start,
        capabilities=lambda port: None, port_open=lambda port: False)
    assert result["status"] == "spawn_failed" and not result["ok"]
    assert json.loads((Path(result["run_directory"]) / "result.json").read_text())["error_type"] == "OSError"


def test_capability_probe_does_not_follow_redirects_or_use_proxies(monkeypatch):
    calls = []
    class Connection:
        def __init__(self, host, port, **kwargs):
            calls.append((host, port))
        def request(self, method, path):
            calls.append((method, path))
        def getresponse(self):
            return SimpleNamespace(status=302)
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(viewer.http.client, "HTTPConnection", Connection)
    assert viewer._capabilities(18000) is None
    assert calls == [("127.0.0.1", 18000), ("GET", "/api/runtime-capabilities"), "closed"]


def test_transient_failure_is_recorded_even_when_retry_succeeds(monkeypatch):
    mod = backup_module()
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    notices = []
    attempts = []
    def run(cmd, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise subprocess.CalledProcessError(255, cmd, stderr="Connection reset token=private-value")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    mod._run(["ssh", "fixture"], run=run, timeout=4, retry_transient=True, retry_notice=notices.append)
    assert len(attempts) == 2 and len(notices) == 1
    assert notices[0]["attempt"] == 1 and notices[0]["error_category"] == "connection_interrupted"
    assert "private-value" not in notices[0]["transport_stderr"]


def test_retry_cannot_reset_the_shared_deadline(monkeypatch):
    mod = backup_module()
    clock = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls = []
    def run(cmd, **kwargs):
        calls.append(kwargs["timeout"])
        clock[0] += 5
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], stderr="timed out")
    with pytest.raises(subprocess.TimeoutExpired):
        mod._run(["ssh", "fixture"], run=run, timeout=4, retry_transient=True)
    assert calls == [2.0] and clock[0] == 5.0


@pytest.fixture
def installer(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows login shortcut integration")
    root = tmp_path / "project space"
    helper = root / "scripts/ensure_research_viewer.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("# do not execute this fixture\n", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_bytes(b"fixture; never executed")
    startup = tmp_path / "startup"
    startup.mkdir()
    script = Path(__file__).resolve().parents[1] / "scripts/install_research_viewer_login_start.ps1"
    return root, tmp_path / "external logs", python, startup, script


def run_installer(fixture, action, *, log_root=None, extra=(), prefix=""):
    root, logs, python, startup, script = fixture
    args = ["-Action", action, "-ProjectRoot", str(root), "-LogRoot", str(log_root or logs),
            "-PythonPath", str(python), "-StartupFolder", str(startup), *extra]
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    if prefix:
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        command += ["-Command", prefix + "\n& " + " ".join(
            str(v) if str(v).startswith("-") else quote(v) for v in [script, *args])]
    else:
        command += ["-File", str(script), *args]
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def test_installer_plan_install_repeat_and_remove_use_only_injected_startup(installer):
    root, logs, python, startup, script = installer
    plan = run_installer(installer, "Plan")
    assert plan.returncode == 0, plan.stderr
    assert json.loads(plan.stdout)["scheduled_tasks_changed"] is False
    assert not list(startup.iterdir()) and not logs.exists()
    installed = run_installer(installer, "Install", extra=["-EnableSelectedQuotes"])
    assert installed.returncode == 0, installed.stderr
    payload = json.loads(installed.stdout)
    assert payload["status"] == "installed_verified" and payload["selected_quote_refresh"]
    link = Path(payload["shortcut"])
    original = link.read_bytes()
    repeated = run_installer(installer, "Install", extra=["-EnableSelectedQuotes"])
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["status"] == "already_installed" and link.read_bytes() == original
    assert not logs.exists() and list(startup.iterdir()) == [link]
    removed = run_installer(installer, "Remove", extra=["-EnableSelectedQuotes"])
    assert removed.returncode == 0, removed.stderr
    assert not list(startup.iterdir()) and not logs.exists()
    assert run_installer(installer, "Remove", extra=["-EnableSelectedQuotes"]).returncode == 0


@pytest.mark.parametrize("child", [False, True])
def test_installer_rejects_project_root_and_descendant_logs(installer, child):
    root, _, _, startup, _ = installer
    result = run_installer(installer, "Plan", log_root=root / "logs" if child else root)
    assert result.returncode != 0 and "outside the project" in result.stderr
    assert not list(startup.iterdir())


def test_installer_preserves_a_shortcut_with_different_permissions(installer):
    installed = run_installer(installer, "Install", extra=["-EnableSelectedQuotes"])
    assert installed.returncode == 0, installed.stderr
    link = Path(json.loads(installed.stdout)["shortcut"])
    original = link.read_bytes()
    for action in ["Install", "Remove"]:
        result = run_installer(installer, action)
        assert result.returncode != 0 and "unrelated Startup shortcut" in result.stderr
        assert link.read_bytes() == original


def test_installer_cleans_partial_link_on_preparation_failure(installer):
    prefix = r'''
function New-Object {
    param([string]$ComObject)
    if ($ComObject -ne 'WScript.Shell') { throw 'unexpected COM request' }
    $shell = [pscustomobject]@{}
    $shell | Add-Member ScriptMethod CreateShortcut {
        param($path)
        $link = [pscustomobject]@{Path=$path;TargetPath='';Arguments='';WorkingDirectory='';Description='';WindowStyle=0}
        $link | Add-Member ScriptMethod Save {
            [IO.File]::WriteAllText($this.Path, 'injected partial shortcut')
            throw 'injected_save_failure'
        }
        return $link
    }
    return $shell
}
'''
    result = run_installer(installer, "Install", prefix=prefix)
    assert result.returncode != 0 and "injected_save_failure" in result.stderr
    assert not list(installer[3].iterdir()) and not installer[1].exists()
