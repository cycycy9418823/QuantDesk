"""Roster transfer uses real local readers/locks and injected SSH, never a network."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid

import pytest

from quantdesk import snapshot_history as history
from quantdesk.full_a_daily_coverage import load_roster_evidence, prepare_roster_version
from quantdesk.interprocess_lock import interprocess_file_lock
from scripts.ops import sync_snapshot_roster as sync

NOW = datetime.fromisoformat("2026-10-31T10:00:04+08:00")


def _publish(root: Path, *, count: int = 5009, day: str = "2026-10-30",
             run: str = "accepted_new", stamp: str | None = None) -> dict:
    prepared = prepare_roster_version(
        [{"symbol": str(600000 + index), "name": "离线名单夹具"} for index in range(count)],
        source="sina.spot_direct", as_of=day, run_id=run,
        accepted_at=stamp or day + "T08:00:00+08:00")
    version = root / prepared["manifest"]["path"]
    version.parent.mkdir(parents=True, exist_ok=True)
    version.write_bytes(prepared["csv_bytes"])
    for name in sync.POINTERS:
        (root / "data/universe" / name).write_text(json.dumps(prepared["manifest"]), encoding="utf-8")
    return prepared


def _bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def transfer(tmp_path):
    local, remote = tmp_path / "local", tmp_path / "remote"
    new = _publish(local)
    old = _publish(remote, count=5000, day="2026-10-12", run="accepted_old")
    legacy = remote / "data/universe/a_share_symbols.csv"
    legacy.write_bytes(b"symbol,name,source,updated_at\n600000,legacy,sina.spot_direct,2026-09-17\n")
    (local / "config").mkdir()
    key, hosts = local / "key_fixture", local / "hosts_fixture"
    key.write_bytes(b"not-a-key-fixture")
    hosts.write_bytes(b"host-fixture")
    config = local / "config/snapshot_roster_sync.json"
    (local / "config/transport.json").write_text(json.dumps({"project_root": str(local), "network": True,
        "remote": "fixture@192.0.2.20", "remote_snapshot_root": "/srv/collector/data/snapshots",
        "ssh_key_path": str(key), "known_hosts_path": str(hosts)}), encoding="utf-8")
    config.write_text(json.dumps({"network": True, "transport_config": "transport.json",
        "receiver_script": "/srv/releases/source-v1/scripts/ops/sync_snapshot_roster.py"}), encoding="utf-8")
    packet = sync.build_roster_packet(local, now=NOW)
    return SimpleNamespace(local=local, remote=remote, new=new, old=old, legacy=legacy,
                           config=config, packet=packet)


def test_sender_receiver_and_actual_collector_use_one_version(transfer, monkeypatch):
    case = transfer
    seen_commands = []
    legacy = case.legacy.read_bytes()

    def transport(command, **kwargs):
        seen_commands.append((command, kwargs))
        receipt = sync.receive_roster_packet(case.remote, kwargs["input"], apply=True, now=NOW)
        return subprocess.CompletedProcess(command, 0, json.dumps(receipt).encode(), b"")

    sent = sync.send_roster(case.local, now=NOW, expected_run_id="accepted_new", run=transport)
    assert sent["status"] == "synced" and sent["remote_receipt"]["status"] == "applied"
    command, args = seen_commands[0]
    assert command[:2] == ["ssh", "-T"] and "StrictHostKeyChecking=yes" in command
    assert "BatchMode=yes" in command and "IdentitiesOnly=yes" in command
    assert command[-2] == "fixture@192.0.2.20" and "--apply" in command[-1]
    assert "/srv/releases/source-v1/scripts/ops/sync_snapshot_roster.py" in command[-1]
    assert "--project-root /srv/collector" in command[-1]
    assert 0 < args["timeout"] <= 45 and "shell" not in args
    assert b"not-a-key-fixture" not in args["input"]
    roster = load_roster_evidence(case.remote, target_date="2026-11-02")
    assert roster["sha256"] == case.new["manifest"]["sha256"] and roster["row_count"] == 5009
    assert roster["as_of"] == "2026-10-30" and roster["accepted_at"] == "2026-10-30T08:00:00+08:00"
    assert case.legacy.read_bytes() == legacy
    previous = json.loads((case.remote / "data/universe/a_share_roster_last_accepted.json").read_bytes())
    assert previous == case.old["manifest"]
    assert sync.COLLECTOR_LOCK == history.LOCK_RELATIVE_PATH
    monkeypatch.setattr(history, "_read_trading_day", lambda *_: (True, "offline_fixture", []))
    requested = []
    current = datetime.fromisoformat("2026-11-02T10:00:04+08:00")

    def fetch(symbols):
        requested.extend(symbols)
        return [{"symbol": code, "price": 10, "open": 10, "high": 10, "low": 10,
            "volume": 100, "amount": 1000, "price_basis": "raw", "quote_date": "2026-11-02",
            "quote_time": "10:00:01"} for code in symbols], "offline_fixture"

    result = history.collect_market_snapshot(case.remote, network=True, now=current,
        clock=lambda: current, fetcher=fetch)
    assert result["written"] and requested == roster["symbols"]
    stored = json.loads((case.remote / result["metadata_path"]).read_bytes())
    assert stored["producer_provenance"]["roster_evidence"]["sha256"] == roster["sha256"]


def test_same_packet_is_idempotent_and_preserves_files_and_dates(transfer):
    first = sync.receive_roster_packet(transfer.remote, transfer.packet, apply=True, now=NOW)
    before = _bytes(transfer.remote)
    again = sync.receive_roster_packet(transfer.remote, transfer.packet, apply=True, now=NOW + timedelta(seconds=1))
    assert first["status"] == "applied" and again["status"] == "already_current"
    assert again["changed"] is False and _bytes(transfer.remote) == before


def test_duplicate_can_repair_a_damaged_latest_pointer_from_exact_fallback(transfer):
    sync.receive_roster_packet(transfer.remote, transfer.packet, apply=True, now=NOW)
    folder = transfer.remote / "data/universe"
    (folder / "a_share_roster_last_accepted.json").write_text(json.dumps(transfer.new["manifest"]))
    (folder / "a_share_roster_latest.json").write_bytes(b"broken")
    result = sync.receive_roster_packet(transfer.remote, transfer.packet, apply=True, now=NOW)
    assert result["status"] == "applied"
    assert load_roster_evidence(transfer.remote, target_date="2026-10-31")["fallback"] is False


@pytest.mark.parametrize("defect", ["csv_hash", "source", "as_of", "updated_at", "duplicate",
    "path_escape", "missing_run_id", "incomplete", "expired", "far_future_expiry", "future_roster",
    "packet_not_object", "manifest_not_object", "bad_base64", "unknown_field"])
def test_bad_packet_cannot_modify_any_remote_file(transfer, defect):
    case = transfer
    envelope = json.loads(case.packet)
    if defect == "csv_hash":
        envelope["manifest"]["sha256"] = "0" * 64
    elif defect in {"source", "as_of", "updated_at", "duplicate"}:
        raw = base64.b64decode(envelope["csv_base64"])
        if defect == "source":
            raw = raw.replace(b"sina.spot_direct", b"unknown_source", 1)
        elif defect == "as_of":
            raw = raw.replace(b"2026-10-30,", b"2026-10-29,", 1)
        elif defect == "updated_at":
            raw = raw.replace(b"T08:00:00", b"T07:00:00", 1)
        else:
            raw += raw.splitlines(keepends=True)[1]
        envelope["csv_base64"] = base64.b64encode(raw).decode()
        envelope["manifest"]["sha256"] = hashlib.sha256(raw).hexdigest()
    elif defect == "path_escape":
        envelope["manifest"]["path"] = "../../outside.csv"
    elif defect == "missing_run_id":
        envelope["manifest"].pop("run_id")
    elif defect in {"incomplete", "future_roster"}:
        prepared = _publish(case.local, count=3 if defect == "incomplete" else 5009,
            day="2026-11-02" if defect == "future_roster" else "2026-10-30", run=defect)
        envelope["manifest"] = prepared["manifest"]
        envelope["csv_base64"] = base64.b64encode(prepared["csv_bytes"]).decode()
    elif defect in {"expired", "far_future_expiry"}:
        envelope["expires_at"] = (NOW + timedelta(seconds=-1 if defect == "expired" else 100)).isoformat()
    elif defect == "packet_not_object":
        envelope = []
    elif defect == "manifest_not_object":
        envelope["manifest"] = []
    elif defect == "bad_base64":
        envelope["csv_base64"] = "!not-base64"
    else:
        envelope["unexpected"] = "must not be accepted"
    before = _bytes(case.remote)
    result = sync.receive_roster_packet(case.remote, json.dumps(envelope).encode(), apply=True, now=NOW)
    assert result["status"] == "rejected" and result["reason_codes"]
    assert _bytes(case.remote) == before


@pytest.mark.parametrize("case_name", ["older_day", "older_time", "same_time_conflict", "count_collapse"])
def test_receiver_rejects_regression_of_current_roster(transfer, case_name):
    case = transfer
    if case_name == "count_collapse":
        _publish(case.remote, count=5500, day="2026-10-12", run="large_previous")
    else:
        _publish(case.remote, day="2026-10-31" if case_name == "older_day" else "2026-10-30",
            run="current_newer", stamp="2026-10-30T09:00:00+08:00" if case_name == "older_time" else None)
    before = _bytes(case.remote / "data/universe")
    result = sync.receive_roster_packet(case.remote, case.packet, apply=True, now=NOW)
    assert result["status"] == "rejected" and result["reason_codes"]
    assert _bytes(case.remote / "data/universe") == before


def test_busy_collector_retains_current_roster_and_lock_bytes(transfer):
    lock = transfer.remote / sync.COLLECTOR_LOCK
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b"existing collector lock\n")
    before = _bytes(transfer.remote)
    roster_before = _bytes(transfer.remote / "data/universe")
    with interprocess_file_lock(lock, timeout_seconds=0):
        result = sync.receive_roster_packet(transfer.remote, transfer.packet, apply=True, now=NOW)
        assert result["status"] == "busy" and result["reason_codes"] == ["collector_lock_busy"]
        assert _bytes(transfer.remote / "data/universe") == roster_before
    # Windows byte-range locks prohibit reading the owned byte through a
    # second handle; compare the lock itself after the owner releases it.
    assert _bytes(transfer.remote) == before


@pytest.mark.parametrize("failed_pointer", list(sync.POINTERS))
def test_pointer_write_failure_restores_previous_visible_version(transfer, monkeypatch, failed_pointer):
    before = _bytes(transfer.remote / "data/universe")
    replace = sync.os.replace

    def fail_once(source, target):
        if Path(target).name == failed_pointer:
            raise OSError("injected write failure")
        return replace(source, target)

    monkeypatch.setattr(sync.os, "replace", fail_once)
    result = sync.receive_roster_packet(transfer.remote, transfer.packet, apply=True, now=NOW)
    assert result["status"] == "failed"
    for path, raw in before.items():
        assert (transfer.remote / "data/universe" / path).read_bytes() == raw
    roster = load_roster_evidence(transfer.remote, target_date="2026-10-31")
    assert roster["sha256"] == transfer.old["manifest"]["sha256"]


def test_deadline_after_pointer_switch_rolls_back(transfer, monkeypatch):
    ticks = [100.0]
    original = sync.load_roster_evidence
    reads = []

    def load(root, **kwargs):
        value = original(root, **kwargs)
        reads.append(value)
        if len(reads) == 2:
            ticks[0] = 120
        return value

    monkeypatch.setattr(sync.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(sync, "load_roster_evidence", load)
    result = sync.receive_roster_packet(transfer.remote, transfer.packet, apply=True, now=NOW)
    assert result["status"] == "failed" and "deadline" in result["reason_codes"][0]
    assert original(transfer.remote, target_date="2026-10-31")["sha256"] == transfer.old["manifest"]["sha256"]


@pytest.mark.parametrize("failed_restore", list(sync.POINTERS))
def test_failed_rollback_reports_unknown_and_attempts_both_restorations(transfer, monkeypatch, failed_restore):
    replace, load = sync._replace_bytes, sync.load_roster_evidence
    rollback = [False]
    attempted = []

    def mismatched_readback(root, **kwargs):
        result = load(root, **kwargs)
        if result.get("sha256") == transfer.new["manifest"]["sha256"]:
            rollback[0] = True
            return {**result, "fallback": True}
        return result

    def fail_restore(path, raw, deadline=None):
        if rollback[0]:
            attempted.append(path.name)
            if path.name == failed_restore:
                raise OSError("injected restore failure")
        return replace(path, raw, deadline)

    monkeypatch.setattr(sync, "load_roster_evidence", mismatched_readback)
    monkeypatch.setattr(sync, "_replace_bytes", fail_restore)
    result = sync.receive_roster_packet(transfer.remote, transfer.packet, apply=True, now=NOW)
    assert result["status"] == "unknown" and result["changed"] is None
    assert result["reason_codes"] == ["roster_rollback_incomplete"]
    assert attempted == list(reversed(sync.POINTERS))


def test_receiver_preview_does_not_create_lock_or_outputs(transfer):
    before = _bytes(transfer.remote)
    result = sync.receive_roster_packet(transfer.remote, transfer.packet, now=NOW)
    assert result["status"] == "dry_run" and _bytes(transfer.remote) == before
    assert not (transfer.remote / sync.COLLECTOR_LOCK).exists()


@pytest.mark.parametrize("mode", ["no_config", "disabled", "transport_disabled", "dry_run", "legacy_only", "wrong_run"])
def test_sender_without_explicit_eligible_input_never_invokes_transport(transfer, mode):
    case = transfer
    if mode == "no_config":
        case.config.unlink()
    elif mode == "disabled":
        config = json.loads(case.config.read_bytes())
        config["network"] = False
        case.config.write_text(json.dumps(config))
    elif mode == "transport_disabled":
        transport_path = case.config.with_name("transport.json")
        config = json.loads(transport_path.read_bytes())
        config["network"] = False
        transport_path.write_text(json.dumps(config))
    elif mode == "legacy_only":
        for name in sync.POINTERS:
            (case.local / "data/universe" / name).unlink()
    called = []
    before = _bytes(case.local)
    result = sync.send_roster(case.local, now=NOW, dry_run=mode == "dry_run",
        expected_run_id="wrong_run" if mode == "wrong_run" else None,
        run=lambda *a, **k: called.append((a, k)))
    assert result["status"] != "synced" and result["network_attempted"] is False and not called
    assert _bytes(case.local) == before


@pytest.mark.parametrize("mode", ["timeout_before_apply", "timeout_after_apply", "wrong_receipt",
    "wrong_manifest", "bad_stdout", "remote_failure"])
def test_sender_does_not_report_success_without_exact_remote_confirmation(transfer, mode):
    case = transfer

    def transport(command, **kwargs):
        if mode == "timeout_before_apply":
            raise subprocess.TimeoutExpired(command, 45)
        receipt = sync.receive_roster_packet(case.remote, kwargs["input"], apply=True, now=NOW)
        if mode == "timeout_after_apply":
            raise subprocess.TimeoutExpired(command, 45)
        if mode == "wrong_receipt":
            receipt["request_id"] = uuid.uuid4().hex
        if mode == "wrong_manifest":
            receipt["manifest"]["sha256"] = "0" * 64
        if mode == "remote_failure":
            receipt["status"] = "failed"
        raw = b"not json" if mode == "bad_stdout" else json.dumps(receipt).encode()
        return subprocess.CompletedProcess(command, 1 if mode == "remote_failure" else 0, raw, b"")

    before = _bytes(case.local)
    result = sync.send_roster(case.local, now=NOW, run=transport)
    assert result["network_attempted"] and result["status"] in {"unknown", "failed"}
    assert result["reason_codes"] and _bytes(case.local) == before
    if mode == "timeout_after_apply":
        def retry(command, **kwargs):
            receipt = sync.receive_roster_packet(case.remote, kwargs["input"], apply=True, now=NOW)
            return subprocess.CompletedProcess(command, 0, json.dumps(receipt).encode(), b"")
        again = sync.send_roster(case.local, now=NOW, run=retry)
        assert again["status"] == "synced" and again["remote_receipt"]["status"] == "already_current"


def test_immutable_version_collision_does_not_replace_existing_bytes(transfer):
    version = transfer.remote / transfer.new["manifest"]["path"]
    version.write_bytes(b"previous interrupted preparation")
    before = _bytes(transfer.remote / "data/universe")
    result = sync.receive_roster_packet(transfer.remote, transfer.packet, apply=True, now=NOW)
    assert result["status"] == "rejected" and "collision" in result["reason_codes"][0]
    assert _bytes(transfer.remote / "data/universe") == before
