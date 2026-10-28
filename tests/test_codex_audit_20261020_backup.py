"""Offline three-book evidence closure and byte-exact isolated restore."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from quantdesk.paper_state_backup import build_backup_evidence_closure, pack_backup_evidence, verify_backup_archive, backup_transport_config


def dump(root: Path, rel: str, value: dict) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def seed(root: Path) -> dict[str, bytes]:
    dump(root, "state/auto_paper_portfolio.json", {"ledger_id": "strategy", "cash": 100000, "positions": []})
    for directory, account in (("ai_trader", "ai_trader_100k"), ("ai_trader_glm", "ai_trader_100k_glm")):
        dump(root, f"state/{directory}/ledger.json", {"ledger_id": account, "cash": 100000, "positions": []})
        body = b'{"model":"actual-model","choices":[]}'
        receipt = dump(root, f"state/{directory}/llm_requests/fixed-request.json", {
            "transport_detail": {"request_id": "fixed", "capture_status": "written"},
            "http_body": {"body_base64": base64.b64encode(body).decode(), "byte_exact": True,
                "retained_bytes": len(body), "received_sha256": hashlib.sha256(body).hexdigest()}})
        dump(root, f"state/{directory}/plan_latest.json", {"ledger_id": account, "plan_id": "plan-fixture",
            "llm": {"response_evidence": {"transport_detail": {"capture_path": str(receipt),
                "capture_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}}}})
        dump(root, f"results/{directory}/decision_run_manifest.json", {"ledger_id": account,
            "as_of": "2026-10-19", "source_sha256": "a" * 64})
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_pack_verify_and_restore_recovers_every_original_body_and_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    originals = seed(source)
    archive = tmp_path / "backup/evidence.tar.gz"
    result = pack_backup_evidence(source, archive)
    assert result["ok"] and result["file_count"] == 9
    assert len(result["references"]) == 2
    restored = tmp_path / "restored"
    result = verify_backup_archive(archive, isolated_restore_root=restored)
    assert result["isolated_restore_verified"] and not result["network_used"]
    assert {p.relative_to(restored).as_posix(): p.read_bytes() for p in restored.rglob("*") if p.is_file()} == originals
    assert {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()} == originals


@pytest.mark.parametrize("damage", ["missing_ref", "wrong_ref_sha", "cross_account", "path_escape", "secret", "body_hash"])
def test_incomplete_or_unsafe_closure_does_not_create_archive(tmp_path: Path, damage: str) -> None:
    source = tmp_path / "source"
    seed(source)
    plan = source / "state/ai_trader/plan_latest.json"
    value = json.loads(plan.read_text(encoding="utf-8"))
    ref = value["llm"]["response_evidence"]["transport_detail"]
    if damage == "missing_ref": Path(ref["capture_path"]).unlink()
    elif damage == "wrong_ref_sha": ref["capture_sha256"] = "0" * 64
    elif damage == "cross_account": value["ledger_id"] = "ai_trader_100k_glm"
    elif damage == "path_escape": ref["capture_path"] = "../private.key"
    elif damage == "secret": value["api_key"] = "fixture-never-copy"
    elif damage == "body_hash":
        receipt = Path(ref["capture_path"])
        body = json.loads(receipt.read_text(encoding="utf-8"))
        body["http_body"]["received_sha256"] = "0" * 64
        receipt.write_text(json.dumps(body), encoding="utf-8")
        ref["capture_sha256"] = hashlib.sha256(receipt.read_bytes()).hexdigest()
    plan.write_text(json.dumps(value), encoding="utf-8")
    archive = tmp_path / "unsafe.tar.gz"
    result = pack_backup_evidence(source, archive)
    assert result["status"] == "blocked" and not archive.exists()


def test_archive_traversal_is_rejected_without_any_restore(tmp_path: Path) -> None:
    archive = tmp_path / "traversal.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        item = tarfile.TarInfo("../outside.json"); item.size = 2
        tar.addfile(item, io.BytesIO(b"{}"))
    result = verify_backup_archive(archive, isolated_restore_root=tmp_path / "restore")
    assert result["status"] == "blocked" and not (tmp_path / "restore").exists()


def test_corrupt_archive_does_not_restore_partial_files(tmp_path: Path) -> None:
    archive = tmp_path / "broken.tar.gz"
    archive.write_bytes(b"bad gz stream")
    result = verify_backup_archive(archive, isolated_restore_root=tmp_path / "restore")
    assert not result["ok"] and not (tmp_path / "restore").exists()


def test_explicit_config_and_network_dry_run_never_read_key_or_start_transport(tmp_path: Path) -> None:
    from scripts.ops import push_backup_to_student as push, pull_snapshots_from_student as pull
    source = tmp_path / "source"
    seed(source)
    config = tmp_path / "backup.json"
    config.write_text(json.dumps({"project_root": str(source), "ssh_key_path": str(tmp_path / "missing-key"),
        "known_hosts_path": str(tmp_path / "missing-hosts"), "remote": "owner@example.invalid", "network": False}), encoding="utf-8")
    original_root = push.CANON
    def forbidden(*_a, **_k):
        pytest.fail("network dry-run started transport")
    assert push.main(["--config", str(config)], run=forbidden) == 0
    assert pull.main(["--config", str(config)], run=forbidden) == 0
    assert push.CANON == original_root and not (tmp_path / "missing-key").exists()


def test_config_rejects_embedded_secret_and_remote_injection(tmp_path: Path) -> None:
    config = tmp_path / "backup.json"
    config.write_text('{"api_key":"fixture"}', encoding="utf-8")
    with pytest.raises(ValueError): backup_transport_config(config)
    config.write_text('{"remote":"owner@host; echo oops"}', encoding="utf-8")
    with pytest.raises(ValueError): backup_transport_config(config)


def test_frozen_original_references_are_resolved_only_inside_detached_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    originals = seed(source)
    origin = tmp_path / "read-only-original"
    plan = source / "state/ai_trader/plan_latest.json"
    value = json.loads(plan.read_text(encoding="utf-8"))
    value["llm"]["response_evidence"]["transport_detail"]["capture_path"] = str(
        origin / "state/ai_trader/llm_requests/fixed-request.json")
    plan.write_text(json.dumps(value), encoding="utf-8")
    page = dump(source, "results/ai_trader/decision_latest.json", {"ledger_id": "ai_trader_100k"})
    dump(source, "results/ai_trader/decision_run_manifest.json", {"ledger_id": "ai_trader_100k",
        "log": str(origin / "results/ai_trader/decision_latest.json")})
    archive = tmp_path / "frozen.tar.gz"
    result = pack_backup_evidence(source, archive, reference_origin_root=origin)
    assert result["ok"] and not origin.exists()
    restored = tmp_path / "restored"
    assert verify_backup_archive(archive, isolated_restore_root=restored)["isolated_restore_verified"]
    assert (restored / page.relative_to(source)).read_bytes() == page.read_bytes()


def test_archive_cannot_drop_manifest_references_to_hide_a_missing_dependency(tmp_path: Path) -> None:
    source = tmp_path / "source"
    seed(source)
    original = tmp_path / "original.tar.gz"
    assert pack_backup_evidence(source, original)["ok"]
    broken = tmp_path / "missing-closure.tar.gz"
    with tarfile.open(original) as incoming, tarfile.open(broken, "w:gz") as outgoing:
        for member in incoming:
            raw = incoming.extractfile(member).read()
            if member.name == "manifest.json":
                value = json.loads(raw)
                value["references"] = []
                raw = json.dumps(value).encode()
            item = tarfile.TarInfo(member.name)
            item.size = len(raw)
            outgoing.addfile(item, io.BytesIO(raw))
    result = verify_backup_archive(broken, isolated_restore_root=tmp_path / "restore")
    assert not result["ok"] and not (tmp_path / "restore").exists()
    assert any("closure_mismatch" in reason for reason in result["errors"])


def test_old_shared_mandate_template_is_preserved_without_relaxing_booked_account(tmp_path: Path) -> None:
    source = tmp_path / "source"
    seed(source)
    path = dump(source, "results/ai_trader_glm/cycle_latest.json", {
        "ledger": {"ledger_id": "ai_trader_100k_glm"},
        "mandate": {"ledger_id": "ai_trader_100k"}})
    result = build_backup_evidence_closure(source)
    assert result["ok"] and result["contents"][path.relative_to(source).as_posix()] == path.read_bytes()
    dump(source, "results/ai_trader_glm/cycle_latest.json", {
        "ledger": {"ledger_id": "ai_trader_100k"},
        "mandate": {"ledger_id": "ai_trader_100k"}})
    assert not build_backup_evidence_closure(source)["ok"]
