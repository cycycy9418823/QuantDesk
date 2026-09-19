"""DS12-10: isolated backup + restore drill for the two paper books.

Offline only. A throwaway source checkout, an external backup destination, and a
throwaway restore target are all created under ``tmp_path``. No real project
state is read or written. The second backup location choice and the owner's real
restore confirmation remain owner/real-site items.
"""

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

REQUIRED = {
    "state/auto_paper_portfolio.json": '{"cash": 1000, "positions": []}\n',
    "state/ai_trader/ledger.json": '{"cash": 2000, "positions": []}\n',
    "state/ai_trader/decision_log.jsonl": '{"event": "buy", "symbol": "600000"}\n',
    "docs/learning_log.md": "# Learning log\n\n- entry one\n",
}


def _seed_source(root: Path) -> None:
    for relative, content in REQUIRED.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_backup_verify_and_restore_drill(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    destination = tmp_path / "external_backup"
    _seed_source(source)

    result = backup_paper_state(source, destination, timestamp="20260913T000000Z")
    assert result.status == "passed"

    manifest = destination / "backup_20260913T000000Z" / "manifest.json"
    assert manifest.is_file()

    verification = verify_paper_state_backup(manifest)
    assert verification.ok is True
    assert verification.mismatches == ()

    plan = build_restore_plan(manifest, tmp_path / "restored")
    assert plan["status"] == "ready_for_owner"
    assert plan["apply"] is False
    assert plan["restore_requires_owner"] is True

    # Simulate the owner-approved restore in isolation: copy every planned item
    # and confirm the restored bytes match the manifest hashes.
    restored = tmp_path / "restored"
    for item in plan["items"]:
        backup_path = Path(item["backup_path"])
        target_path = Path(item["target_path"])
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(backup_path.read_bytes())
        assert _sha256(target_path) == item["sha256"]

    for relative, content in REQUIRED.items():
        assert (restored / relative).read_text(encoding="utf-8") == content


def test_verify_detects_tampered_backup_artifact(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    destination = tmp_path / "external_backup"
    _seed_source(source)
    backup_paper_state(source, destination, timestamp="20260913T000000Z")
    manifest = destination / "backup_20260913T000000Z" / "manifest.json"
    plan = build_restore_plan(manifest, tmp_path / "restored")
    item = next(entry for entry in plan["items"] if entry["name"] == "strategy_ledger")
    Path(item["backup_path"]).write_text('{"cash": 0, "positions": []}\n', encoding="utf-8")

    verification = verify_paper_state_backup(manifest)
    assert verification.ok is False
    assert any("strategy_ledger" in item for item in verification.mismatches)


def test_backup_missing_required_artifact_fails_visible(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    _seed_source(source)
    (source / "state/ai_trader/ledger.json").unlink()
    destination = tmp_path / "external_backup"

    result = backup_paper_state(source, destination, timestamp="20260913T000000Z")
    assert result.status != "passed"

    manifest = destination / "backup_20260913T000000Z" / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    missing = {row["name"] for row in payload["artifacts"] if row["status"] == "missing"}
    assert "ai_ledger" in missing


def test_destination_inside_source_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    _seed_source(source)

    with pytest.raises(ValueError):
        backup_paper_state(source, source / "backup_here", timestamp="20260913T000000Z")
