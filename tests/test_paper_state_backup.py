from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quantdesk.paper_state_backup import (
    backup_paper_state,
    build_restore_plan,
    verify_paper_state_backup,
)


def _source_tree(root: Path, *, with_learning_log: bool = True) -> dict[str, Path]:
    files = {
        "strategy_ledger": root / "state" / "auto_paper_portfolio.json",
        "ai_ledger": root / "state" / "ai_trader" / "ledger.json",
        "decision_log": root / "state" / "ai_trader" / "decision_log.jsonl",
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    files["strategy_ledger"].write_text('{"ledger_id":"strategy"}\n', encoding="utf-8")
    files["ai_ledger"].write_text('{"ledger_id":"ai_trader_100k"}\n', encoding="utf-8")
    files["decision_log"].write_text('{"decision_id":"d1"}\n', encoding="utf-8")
    if with_learning_log:
        learning = root / "docs" / "learning_log.md"
        learning.parent.mkdir(parents=True, exist_ok=True)
        learning.write_text("# This week I learned\n", encoding="utf-8")
        files["learning_log"] = learning
    return files


def test_backup_requires_an_explicit_destination() -> None:
    with pytest.raises(TypeError):
        backup_paper_state(Path("source"))  # type: ignore[call-arg]


def test_backup_copies_two_ledgers_records_and_learning_log_with_hashes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "external-backup"
    expected = _source_tree(source)
    before = {name: path.read_bytes() for name, path in expected.items()}

    result = backup_paper_state(source, destination, timestamp="2026-09-06T12:00:00Z")

    assert result.ok is True
    assert result.status == "passed"
    assert result.manifest_path == destination / "backup_20260906T120000Z" / "manifest.json"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["restore_requires_owner"] is True
    assert "paper-only" in manifest["disclaimer"].lower()
    assert {row["name"] for row in manifest["artifacts"]} == set(expected)
    for row in manifest["artifacts"]:
        expected_bytes = before[row["name"]]
        assert row["status"] == "copied"
        assert row["bytes"] == len(expected_bytes)
        assert row["sha256"] == hashlib.sha256(expected_bytes).hexdigest()
        assert Path(row["backup_path"]).read_bytes() == expected_bytes
        assert Path(row["source_path"]).read_bytes() == expected_bytes


def test_missing_learning_log_is_failed_and_never_claims_success(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "external-backup"
    _source_tree(source, with_learning_log=False)

    result = backup_paper_state(source, destination, timestamp="2026-09-06T12:00:00Z")

    assert result.ok is False
    assert result.status == "failed"
    assert "learning_log" in result.missing
    assert "learning_log" in result.warnings


def test_destination_must_be_outside_source_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _source_tree(source)

    with pytest.raises(ValueError, match="outside"):
        backup_paper_state(source, source / "backup")


def test_verify_detects_tampering_and_restore_plan_does_not_apply(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "external-backup"
    _source_tree(source)
    result = backup_paper_state(source, destination, timestamp="2026-09-06T12:00:00Z")
    ledger_backup = next(
        Path(row["backup_path"])
        for row in result.manifest["artifacts"]
        if row["name"] == "strategy_ledger"
    )
    ledger_backup.write_text("tampered\n", encoding="utf-8")

    verification = verify_paper_state_backup(result.manifest_path)
    assert verification.ok is False
    assert verification.status == "failed"
    assert "strategy_ledger" in verification.mismatches

    target = tmp_path / "restore-target"
    plan = build_restore_plan(result.manifest_path, target)
    assert plan["status"] == "verification_failed"
    assert plan["apply"] is False
    assert not target.exists()


def test_valid_backup_can_be_verified_and_moved_to_another_directory(tmp_path: Path) -> None:
    import shutil

    source = tmp_path / "source"
    _source_tree(source)
    result = backup_paper_state(source, tmp_path / "backup")
    assert verify_paper_state_backup(result.manifest_path).ok is True
    moved = tmp_path / "moved"
    shutil.copytree(result.backup_dir, moved)
    assert verify_paper_state_backup(moved / "manifest.json").ok is True
    target = tmp_path / "restore"
    plan = build_restore_plan(moved / "manifest.json", target)
    assert plan["status"] == "ready_for_owner"
    assert len(plan["items"]) == 4
    assert not target.exists()


def test_backup_detects_source_change_while_copying(tmp_path: Path, monkeypatch) -> None:
    from quantdesk import paper_state_backup as backup

    source = tmp_path / "source"
    _source_tree(source)
    original = backup._atomic_copy

    def changing_copy(source_path, target):
        original(source_path, target)
        if source_path.name == "ledger.json":
            source_path.write_text('{"changed":true}', encoding="utf-8")

    monkeypatch.setattr(backup, "_atomic_copy", changing_copy)
    result = backup.backup_paper_state(source, tmp_path / "backup")
    assert result.ok is False
    assert "source_changed_during_backup:ai_ledger" in result.warnings
    assert verify_paper_state_backup(result.manifest_path).ok is False


def test_empty_or_traversing_manifest_never_verifies(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _source_tree(source)
    result = backup_paper_state(source, tmp_path / "backup")
    payload = dict(result.manifest)
    payload["artifacts"] = []
    assert verify_paper_state_backup(payload).ok is False
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"][0]["relative_path"] = "../../secret.txt"
    assert verify_paper_state_backup(payload).ok is False
    assert build_restore_plan(payload, tmp_path / "restore")["items"] == []


def test_dry_run_does_not_create_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _source_tree(source)
    destination = tmp_path / "backup"
    result = backup_paper_state(source, destination, dry_run=True)
    assert result.status == "planned"
    assert not destination.exists()
    assert verify_paper_state_backup(result.manifest).ok is False


def test_same_backup_id_never_overwrites_previous_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _source_tree(source)
    backup_paper_state(source, tmp_path / "backup", timestamp="2026-09-06T12:00:00Z")
    with pytest.raises(FileExistsError):
        backup_paper_state(source, tmp_path / "backup", timestamp="2026-09-06T12:00:00Z")


def test_cli_requires_destination_and_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    from scripts.backup_paper_state import main

    source = tmp_path / "source"
    _source_tree(source)
    with pytest.raises(SystemExit) as missing:
        main(["--source-root", str(source)])
    assert missing.value.code == 2
    destination = tmp_path / "backup"
    assert main(["--source-root", str(source), "--destination", str(destination), "--dry-run"]) == 0
    assert not destination.exists()
    capsys.readouterr()
