from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_ops_backup_push import FakeProc, load_mod, seed


@dataclass
class Clock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _prepare_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = load_mod()
    seed(mod, tmp_path)
    clock = Clock()
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    settings = mod.CANON / "config/project_recovery_backup.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"enabled": True, "source_scope": "current_working_tree"}), encoding="utf-8")

    def pack(source, target, *, source_scope):
        assert source == mod.CANON and source_scope == "current_working_tree"
        target.write_bytes(b"offline transfer fixture")
        return {"ok": True, "archive_sha256": "a" * 64, "source_head": "b" * 40}

    monkeypatch.setattr(mod.recovery, "pack_project_recovery", pack)
    return mod, clock


def _exercise_recovery(mod, upload):
    calls = []

    def run(cmd, **kwargs):
        text = " ".join(map(str, cmd))
        calls.append(text)
        if cmd[0] == "scp" and str(cmd[-1]).endswith("/project_recovery.tar.gz"):
            upload(cmd, kwargs["timeout"])
        return FakeProc(returncode=0, stdout="MATCH\n" if "python3" in text else "", stderr="")

    def listing(*args, **kwargs):
        calls.append("LIST")
        return ""

    code = mod.main([], run=run, check_output=listing)
    final = json.loads(mod.LOG.read_text(encoding="utf-8").splitlines()[-1])
    return code, final, calls


def test_recovery_upload_keeps_full_budget_on_healthy_slow_transfer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, clock = _prepare_recovery(tmp_path, monkeypatch)
    budgets = []

    def upload(cmd, timeout):
        budgets.append(timeout)
        if timeout < 600:
            clock.now += timeout
            raise subprocess.TimeoutExpired(cmd, timeout)
        clock.now += 600

    code, final, calls = _exercise_recovery(mod, upload)
    assert code == 0
    assert budgets == pytest.approx([900])
    assert final["project_recovery"]["remote_verified"] is True
    assert final["all_hashes_match"] is True
    assert "LIST" in calls


def test_early_disconnect_retries_only_remaining_recovery_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, clock = _prepare_recovery(tmp_path, monkeypatch)
    budgets = []

    def upload(cmd, timeout):
        budgets.append(timeout)
        if len(budgets) == 1:
            clock.now += 80
            raise subprocess.CalledProcessError(255, cmd, stderr="Connection reset by peer")
        assert timeout >= 600
        clock.now += 600

    code, final, _ = _exercise_recovery(mod, upload)
    assert code == 0
    assert budgets == pytest.approx([900, 819.5])
    assert clock.now < 900
    assert final["all_hashes_match"] is True


def test_exhausted_recovery_budget_never_starts_retry_or_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, clock = _prepare_recovery(tmp_path, monkeypatch)
    budgets = []

    def upload(cmd, timeout):
        budgets.append(timeout)
        clock.now += timeout
        raise subprocess.TimeoutExpired(cmd, timeout)

    code, final, calls = _exercise_recovery(mod, upload)
    assert code == 5
    assert budgets == pytest.approx([900])
    assert final["paper_backup_verified"] is True
    assert final["all_hashes_match"] is False
    assert "LIST" not in calls
    rows = [json.loads(line) for line in mod.LOG.read_text(encoding="utf-8").splitlines()]
    assert not any(row.get("event") == "transport_retry" for row in rows)


@pytest.mark.parametrize("stderr, category", [
    ("Permission denied (publickey)", "authentication_failed"),
    ("Host key verification failed", "host_verification_failed"),
])
def test_recovery_security_failure_does_not_retry_or_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        stderr: str, category: str) -> None:
    mod, clock = _prepare_recovery(tmp_path, monkeypatch)
    budgets = []

    def upload(cmd, timeout):
        budgets.append(timeout)
        clock.now += 1
        raise subprocess.CalledProcessError(255, cmd, stderr=stderr)

    code, final, calls = _exercise_recovery(mod, upload)
    assert code == 2 and len(budgets) == 1
    assert final["error_category"] == category
    assert final["all_hashes_match"] is False
    assert "LIST" not in calls


def test_local_route_timeout_has_own_category_and_bounded_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_mod()
    clock = Clock()
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    budgets, notices = [], []

    def run(cmd, **kwargs):
        budgets.append(kwargs["timeout"])
        if len(budgets) == 1:
            clock.now += 15
            raise subprocess.CalledProcessError(255, cmd,
                stderr="physical_ssh_transport:OSError:physical_route_inventory_timeout:15s")
        return FakeProc(returncode=0, stdout="", stderr="")

    mod._run(["ssh", "offline fixture"], run=run, timeout=120,
             retry_transient=True, retry_notice=notices.append)
    assert budgets == pytest.approx([60, 104.5])
    assert notices[0]["error_category"] == "physical_route_inventory_timeout"
    assert clock.now < 120


def test_small_transport_keeps_existing_split_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_mod()
    clock = Clock()
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    budgets = []

    def run(cmd, **kwargs):
        budgets.append(kwargs["timeout"])
        return FakeProc(returncode=0, stdout="", stderr="")

    mod._run(["scp", "small offline fixture"], run=run, timeout=120, retry_transient=True)
    assert budgets == pytest.approx([60])
