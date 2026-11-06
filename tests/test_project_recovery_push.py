from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_ops_backup_push import FakeProc, load_mod, seed


@pytest.mark.parametrize("outcome", ["ok", "pack_failed", "remote_failed"])
@pytest.mark.parametrize("source_scope", ["current_tree", "current_working_tree"])
def test_enabled_recovery_requires_separate_success_before_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    outcome: str, source_scope: str) -> None:
    mod = load_mod()
    seed(mod, tmp_path)
    settings = mod.CANON / "config/project_recovery_backup.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({'enabled': True, 'source_scope': source_scope}), encoding="utf-8")
    calls = []
    expected_scope = source_scope
    def pack(source, target, *, source_scope):
        assert source == mod.CANON
        assert source_scope == expected_scope
        if outcome == "pack_failed":
            return {"ok": False, "error": "fixture-pack-failed"}
        target.write_bytes(b"fixture-transfer-only")
        return {"ok": True, "archive_sha256": "a" * 64, "source_head": "b" * 40}
    monkeypatch.setattr(mod.recovery, "pack_project_recovery", pack)
    def run(cmd, **kwargs):
        text = " ".join(map(str, cmd))
        calls.append((text, kwargs.get("timeout")))
        if "python3" in text:
            return FakeProc(returncode=0, stdout="NO_MATCH\n" if outcome == "remote_failed" and "verify_project_recovery" in text else "MATCH\n", stderr="")
        return FakeProc(returncode=0, stdout="", stderr="")
    def listing(*args, **kwargs):
        calls.append(("LIST", 0))
        return ""
    code = mod.main([], run=run, check_output=listing)
    result = json.loads(mod.LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert result["paper_backup_verified"] is True
    assert result["all_hashes_match"] is (outcome == "ok")
    assert (code == 0) is (outcome == "ok")
    assert any(text == "LIST" for text, _ in calls) is (outcome == "ok")
    if outcome == "ok":
        assert result["project_recovery"]["status"] == "remote_verified"
        assert any("verify_project_recovery.py" in text and timeout == 900 for text, timeout in calls)
    else:
        assert not any("rm -rf" in text for text, _ in calls)


def test_missing_setting_keeps_existing_ledger_backup_without_recovery_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_mod()
    seed(mod, tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("unapproved recovery supplement was created")
    monkeypatch.setattr(mod.recovery, "pack_project_recovery", forbidden)
    calls = []
    def run(cmd, **kwargs):
        text = " ".join(map(str, cmd))
        calls.append(text)
        return FakeProc(returncode=0, stdout="MATCH\n" if "python3" in text else "", stderr="")
    assert mod.main(["--no-prune"], run=run) == 0
    row = json.loads(mod.LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert row["project_recovery"]["status"] == "disabled"
    assert not any("project_recovery.tar.gz" in text for text in calls)


def test_offline_entrypoint_never_calls_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_mod()
    seed(mod, tmp_path)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"project_root": str(mod.CANON), "network": False}), encoding="utf-8")
    expected = tmp_path / "offline.tar.gz"
    def pack(source, target, **kwargs):
        assert source == mod.CANON and Path(target) == expected
        return {"ok": True, "network_used": False}
    monkeypatch.setattr(mod.recovery, "pack_project_recovery", pack)
    def forbidden(*args, **kwargs):
        pytest.fail("offline pack started transport")
    assert mod.main(["--config", str(config), "--offline-recovery-pack", str(expected)], run=forbidden) == 0


def test_offline_working_source_scope_is_passed_without_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_mod()
    seed(mod, tmp_path)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'project_root': str(mod.CANON), 'network': False}), encoding='utf-8')
    target = tmp_path / 'working-source.tar.gz'
    calls = []
    def pack(source, destination, **kwargs):
        calls.append(kwargs['source_scope'])
        assert source == mod.CANON and Path(destination) == target
        return {'ok': True, 'network_used': False}
    monkeypatch.setattr(mod.recovery, 'pack_project_recovery', pack)
    def forbidden(*args, **kwargs):
        pytest.fail('Offline working-source pack started transport')
    assert mod.main(['--config', str(config), '--offline-recovery-pack', str(target),
                     '--source-scope', 'current_working_tree'], run=forbidden) == 0
    assert calls == ['current_working_tree']
