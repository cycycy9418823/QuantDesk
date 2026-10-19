from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from scripts.run_tests_isolated import build_test_copy


def test_runtime_write_fence_rejects_write_before_file_changes(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    (protected / "results").mkdir(parents=True)
    evidence = protected / "results" / "real.json"
    evidence.write_text("original", encoding="utf-8")
    code = "from pathlib import Path; from quantdesk.maintenance.test_safety import install_test_write_fence; import sys; p=Path(sys.argv[1]); install_test_write_fence([p]); (p/'results'/'real.json').write_text('polluted')"
    result = subprocess.run([sys.executable, "-B", "-c", code, str(protected)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "test_runtime_write_blocked" in result.stderr
    assert evidence.read_text(encoding="utf-8") == "original"


def test_isolated_copy_contains_current_source_and_no_live_runtime(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    # Reuse a real HEAD only for source identification; the test does not commit.
    (source / "runner.py").write_text("print('current diff')", encoding="utf-8")
    (source / "results").mkdir()
    (source / "results" / "real.json").write_text("asset", encoding="utf-8")
    (source / "tests" / "fixtures").mkdir(parents=True)
    (source / "tests" / "fixtures" / "tiny.csv").write_text("x\n1\n", encoding="utf-8")
    (source / "quantdesk" / "data").mkdir(parents=True)
    (source / "quantdesk" / "data" / "schema_normalizer.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "data" / "daily").mkdir(parents=True)
    (source / "data" / "daily" / "000001.csv").write_text("real market asset", encoding="utf-8")
    from unittest.mock import patch
    original = subprocess.run

    def run(command, **kwargs):
        if command[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="fixture-base\n")
        return original(command, **kwargs)

    with patch("scripts.run_tests_isolated.subprocess.run", side_effect=run):
        sandbox, manifest = build_test_copy(source, tmp_path / "outputs", include_git_metadata=False)
    assert (sandbox / "runner.py").read_text() == "print('current diff')"
    assert not (sandbox / "results").exists()
    assert (sandbox / "tests" / "fixtures" / "tiny.csv").exists()
    assert (sandbox / "quantdesk" / "data" / "schema_normalizer.py").exists()
    assert not (sandbox / "data").exists()
    assert manifest["real_runtime_copied"] is False
    assert json.loads((sandbox / ".quantdesk-test-sandbox.json").read_text())["source_root"] == str(source.resolve())


@pytest.mark.parametrize("query", ["mode=rwc", "note=mode=ro&mode=rwc"])
def test_sqlite_uri_cannot_bypass_protected_file_check(tmp_path: Path, query: str) -> None:
    protected = tmp_path / "protected space"
    protected.mkdir()
    target = protected / "ledger.sqlite"
    code = "from pathlib import Path; import sqlite3,sys; from quantdesk.maintenance.test_safety import install_test_write_fence; p=Path(sys.argv[1]); install_test_write_fence([p],all_files=True); sqlite3.connect((p/'ledger.sqlite').as_uri()+'?'+sys.argv[2],uri=True)"
    result = subprocess.run([sys.executable, "-B", "-c", code, str(protected), query], capture_output=True, text=True)
    assert result.returncode != 0
    assert "test_runtime_write_blocked" in result.stderr
    assert not target.exists()


def test_indirect_native_mutations_and_python_bootstrap_bypass_are_rejected_without_running(tmp_path: Path, monkeypatch) -> None:
    from quantdesk.maintenance import test_safety
    hooks = []
    monkeypatch.setattr(test_safety.sys, "addaudithook", hooks.append)
    test_safety.install_test_write_fence([tmp_path / "fake-protected"], all_files=True)
    hook = hooks[-1]
    for executable, arguments in [
        ("cmd.exe", ["cmd.exe", "/c", "taskkill /PID 123"]),
        ("powershell.exe", ["powershell.exe", "-Command", "taskkill /PID 123"]),
        (sys.executable, [sys.executable, "-S", "-c", "pass"]),
        (sys.executable, [sys.executable, "-I", "-c", "pass"]),
    ]:
        with pytest.raises(PermissionError):
            hook("subprocess.Popen", (executable, arguments, None, dict(os.environ)))
    with pytest.raises(PermissionError, match="missing_fence_bootstrap"):
        hook("subprocess.Popen", (sys.executable, [sys.executable, "-c", "pass"], None, {}))
    with pytest.raises(PermissionError, match="os_system_blocked"):
        hook("os.system", (b"taskkill /PID 123",))


@pytest.mark.parametrize("flags", [
    ["-S"], ["-I"], ["-E"], ["-X", "utf8", "-S"], ["-B", "-IS"],
    ["-BX", "utf8", "-S"], ["-BW", "ignore", "-I"], ["-BXutf8", "-E"],
])
def test_actual_python_launch_cannot_disable_the_bootstrap(flags: list[str]) -> None:
    # Use real Popen argument normalization; on Windows the audit event has
    # executable=None and a string even though this caller supplies a list.
    with pytest.raises(PermissionError, match="bootstrap_bypass"):
        subprocess.run([sys.executable, *flags, "-c", "pass"], check=True)


@pytest.mark.parametrize("missing", ["QUANTDESK_TEST_FENCE_STATE", "PYTHONPATH"])
def test_actual_python_launch_requires_inherited_protection(missing: str) -> None:
    env = dict(os.environ)
    env.pop(missing, None)
    with pytest.raises(PermissionError, match="missing_fence_bootstrap"):
        subprocess.run([sys.executable, "-B", "-c", "pass"], env=env, check=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows Popen audit command-line representation")
def test_windows_audit_rejects_native_commands_without_running_them(tmp_path: Path, monkeypatch) -> None:
    from quantdesk.maintenance import test_safety
    hooks = []
    monkeypatch.setattr(test_safety.sys, "addaudithook", hooks.append)
    test_safety.install_test_write_fence([tmp_path / "fake-audit-root"], all_files=True)
    hook = hooks[-1]
    for command in [
        r'"C:\Windows\System32\taskkill.exe" /PID 123',
        r'"C:\Windows\System32\cmd.exe" /c "taskkill /PID 123"',
        r'"C:\Windows\System32\OpenSSH\ssh.exe" example.invalid',
        r'"C:\Program Files\Python\python.exe" -X utf8 -I -c "pass"',
    ]:
        with pytest.raises(PermissionError):
            hook("subprocess.Popen", (None, command, None, dict(os.environ)))
    with pytest.raises(PermissionError, match="bootstrap_bypass"):
        hook("subprocess.Popen", (sys.executable, subprocess.list2cmdline([sys.executable, "-S", "-c", "pass"]), None, dict(os.environ)))


def test_fixture_and_extra_fences_survive_three_python_generations(tmp_path: Path) -> None:
    from tests._isolated_project import IsolatedProject
    from quantdesk.maintenance import test_safety
    source = tmp_path / "minimal_source"
    package = source / "quantdesk" / "maintenance"
    package.mkdir(parents=True)
    (source / "quantdesk" / "__init__.py").write_text("# disposable package\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "test_safety.py").write_bytes(Path(test_safety.__file__).read_bytes())
    protected = tmp_path / "extra_protected"
    protected.mkdir()
    (protected / "evidence.txt").write_text("original", encoding="utf-8")
    fixture = tmp_path / "startup_fixture.py"
    fixture.write_text('''import datetime, json, os, sys, types
from pathlib import Path
from zoneinfo import ZoneInfo
root = Path(os.environ["FIXTURE_ROOT"])
class Frozen(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 12, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)
datetime.datetime = Frozen
provider = types.ModuleType("fixture_provider")
provider.quote = lambda: {"price": 10, "source": "synthetic_fixture"}
sys.modules[provider.__name__] = provider
with (root / "fixture_loads.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"pid": os.getpid()}) + "\\n")
''', encoding="utf-8")
    with IsolatedProject(source, temp_parent=tmp_path) as project:
        root = project.root
        assert root is not None
        project.install_startup_fixture(fixture)
        (root / "different_fixture.py").write_text("raise AssertionError('must not execute')", encoding="utf-8")
        probe = root / "probe.py"
        probe.write_text('''import datetime, json, os, socket, subprocess, sys
from pathlib import Path
import quantdesk, fixture_provider
from quantdesk.maintenance import test_safety
generation = int(sys.argv[1])
root, protected = Path(os.environ["FIXTURE_ROOT"]), Path(sys.argv[2])
assert Path(quantdesk.__file__).resolve() == root / "quantdesk/__init__.py"
assert datetime.datetime.now().hour == 18
assert fixture_provider.quote() == {"price": 10, "source": "synthetic_fixture"}
if generation == 0:
    test_safety.install_test_write_fence([protected], all_files=True)
else:
    # Re-publication after the normal package import must retain the extra
    # ancestor root loaded by the file-spec startup module.
    test_safety.install_test_write_fence([root / ("additional_" + str(generation))], all_files=True)
test_safety._run_startup_fixture()
old_fixture = os.environ["QUANTDESK_TEST_STARTUP_FIXTURE"]
os.environ["QUANTDESK_TEST_STARTUP_FIXTURE"] = str(root / "different_fixture.py")
try:
    test_safety._run_startup_fixture()
except ValueError as error:
    assert "fixture_changed" in str(error)
else:
    raise AssertionError("fixture replacement was accepted")
os.environ["QUANTDESK_TEST_STARTUP_FIXTURE"] = old_fixture
for action in (lambda: (protected / "evidence.txt").write_text("polluted"),
               lambda: socket.getaddrinfo("example.invalid", 443)):
    try:
        action()
    except PermissionError:
        pass
    else:
        raise AssertionError("inherited guard missing")
if generation < 2:
    child = subprocess.run([sys.executable, "-B", __file__, str(generation + 1), str(protected)],
                           capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
print(json.dumps({"generation": generation, "package": quantdesk.__file__}))
''', encoding="utf-8")
        env = project.subprocess_env()
        env["FIXTURE_ROOT"] = str(root)
        result = subprocess.run([sys.executable, "-B", str(probe), "0", str(protected)],
                                cwd=root, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        records = [json.loads(line) for line in (root / "fixture_loads.jsonl").read_text().splitlines()]
        assert len(records) == len({row["pid"] for row in records}) == 3
    assert (protected / "evidence.txt").read_text(encoding="utf-8") == "original"
