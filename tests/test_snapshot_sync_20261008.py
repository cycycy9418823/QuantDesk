"""Snapshot delivery counterexamples; all remote/local files live in tmp_path."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


DAY = "2026-10-08"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ops/pull_snapshots_from_student.py"


def load_mod():
    spec = importlib.util.spec_from_file_location("snapshot_sync_oct8", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def add_slot(root: Path, slot: str, *, day: str = DAY, count: int | None = None):
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    raw = f"symbol,price,quote_date,quote_time\n600000,10,{day},{slot[:2]}:{slot[2:]}:00\n".encode()
    (folder / f"{slot}.csv").write_bytes(raw)
    meta = {"schema_version": "a0_snapshot_history_v1", "trade_date": day,
            "slot_time": slot, "csv_sha256": hashlib.sha256(raw).hexdigest(),
            "snapshot_path": f"data/snapshots/{day}/{slot}.csv", "status": "partial",
            "source": "fixture", "universe_sha256": "fixture-roster"}
    (folder / f"{slot}.json").write_text(json.dumps(meta), encoding="utf-8")
    slots = sorted(p.stem for p in folder.glob("*.csv"))
    manifest = {"schema_version": "a0_snapshot_history_v1", "trade_date": day,
                "collected_slot_count": len(slots) if count is None else count,
                "collected_slots": slots, "completed_slots": [], "partial_slots": slots,
                "universe_sha256": "fixture-roster", "slot_attempts": {},
                "slot_universes": {s: {"sha256": "fixture-roster"} for s in slots}}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


class LocalRemote:
    """Drive the old transport or the new reader against the same real fixture."""

    def __init__(self, mod, root: Path):
        self.mod = mod
        self.root = root
        self.after_pack = None
        self.use_emitted_program = False
        self.transferred: list[list[str]] = []
        self.calls: list[str] = []

    def __call__(self, cmd, **kwargs):
        command = cmd[-1]
        day = DAY
        if command.startswith("ls "):
            self.calls.append("ls")
            return subprocess.CompletedProcess(cmd, 0, stdout=DAY + "\n", stderr="")
        if command.startswith("cat "):
            self.calls.append("cat")
            return subprocess.CompletedProcess(cmd, 0, stdout=(self.root/day/"manifest.json").read_text(), stderr="")
        if command.startswith("find "):
            self.calls.append("find")
            return subprocess.CompletedProcess(cmd, 0, stdout=str(sum(p.is_file() for p in (self.root/day).rglob("*"))), stderr="")
        out = io.BytesIO()
        if command.startswith("python3 "):
            self.calls.append("sync")
            known = json.loads(kwargs["input"])
            if self.use_emitted_program:
                program = shlex.split(command)
                program[0] = sys.executable
                program[-2] = str(self.root)
                program.insert(1, "-B")
                result = subprocess.run(program, input=kwargs["input"], capture_output=True, timeout=15)
                if result.returncode:
                    return result
                out.write(result.stdout)
            else:
                try:
                    self.mod._pack_snapshot(self.root/day, known, out)
                except (OSError, ValueError, TypeError) as exc:
                    return subprocess.CompletedProcess(cmd, 2, stdout=b"", stderr=str(exc).encode())
        else:
            assert command.startswith("tar ")
            self.calls.append("tar")
            with tarfile.open(fileobj=out, mode="w:gz") as archive:
                for p in sorted((self.root/day).rglob("*")):
                    if p.is_file():
                        archive.add(p, arcname=f"{day}/{p.relative_to(self.root/day).as_posix()}")
        raw = out.getvalue()
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            self.transferred.append([m.name for m in archive.getmembers() if m.isfile()])
        if self.after_pack:
            hook, self.after_pack = self.after_pack, None
            hook()
        return subprocess.CompletedProcess(cmd, 0, stdout=raw, stderr=b"")


@pytest.fixture
def environment(tmp_path):
    mod = load_mod()
    remote = tmp_path / "remote"
    add_slot(remote, "0930")
    local = tmp_path / "local"
    (local/"data"/"snapshots").mkdir(parents=True)
    args = {"canon": local, "tmp": tmp_path/"staging", "log": tmp_path/"pull.log", "day": DAY}
    return mod, remote, local/"data"/"snapshots"/DAY, args, LocalRemote(mod, remote)


def test_cloud_advancing_after_download_does_not_reject_complete_version(environment):
    mod, remote, local, args, runner = environment
    frozen_manifest = (remote/DAY/"manifest.json").read_bytes()
    runner.after_pack = lambda: add_slot(remote, "0935")
    code, _ = mod.pull(run=runner, **args)
    assert code == 0
    assert json.loads((local/"manifest.json").read_text())["collected_slots"] == ["0930"]
    assert (local/"manifest.json").read_bytes() == frozen_manifest
    assert (local/"0930.csv").is_file()


def test_second_slot_transfers_only_new_files(environment):
    mod, remote, local, args, runner = environment
    assert mod.pull(run=runner, **args)[0] == 0
    old_mtime = (local/"0930.csv").stat().st_mtime_ns
    add_slot(remote, "0935")
    assert mod.pull(run=runner, **args)[0] == 0
    assert f"{DAY}/0930.csv" not in runner.transferred[-1]
    assert f"{DAY}/0935.csv" in runner.transferred[-1]
    assert (local/"0930.csv").stat().st_mtime_ns == old_mtime


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_identical_manifest_does_not_hide_damaged_local_csv(environment, damage):
    mod, remote, local, args, runner = environment
    assert mod.pull(run=runner, **args)[0] == 0
    if damage == "missing":
        (local/"0930.csv").unlink()
    else:
        (local/"0930.csv").write_bytes(b"damaged\n")
    assert mod.pull(run=runner, **args)[0] == 0
    assert (local/"0930.csv").read_bytes() == (remote/DAY/"0930.csv").read_bytes()


def test_manifest_count_alone_is_not_a_complete_local_history(tmp_path):
    mod = load_mod()
    folder = tmp_path / DAY
    folder.mkdir()
    (folder/"manifest.json").write_text(json.dumps({"collected_slot_count": 48}), encoding="utf-8")
    assert mod._local_complete(folder) is False


def test_bad_remote_csv_keeps_previous_local_version(environment):
    mod, remote, local, args, runner = environment
    assert mod.pull(run=runner, **args)[0] == 0
    before = {p.relative_to(local).as_posix(): p.read_bytes() for p in local.rglob("*") if p.is_file()}
    add_slot(remote, "0935")
    (remote/DAY/"0935.csv").write_bytes(b"does not match sidecar\n")
    code, payload = mod.pull(run=runner, **args)
    assert code != 0
    assert payload["pulled_at"] is None
    assert {p.relative_to(local).as_posix(): p.read_bytes() for p in local.rglob("*") if p.is_file()} == before


def directory_bytes(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


@pytest.mark.parametrize("broken", [b"{", b"[]", b'{"collected_slots":["BROKEN"]}',
                                    b'{"schema_version":"bad","collected_slots":[]}'])
def test_damaged_local_manifest_recovers_without_discarding_original(environment, broken):
    mod, remote, local, args, runner = environment
    assert mod.pull(run=runner, **args)[0] == 0
    csv_before = (local/"0930.csv").read_bytes()
    (local/"manifest.json").write_bytes(broken)
    code, payload = mod.pull(run=runner, **args)
    assert code == 0, payload
    assert (local/"manifest.json").read_bytes() == (remote/DAY/"manifest.json").read_bytes()
    assert (local/"0930.csv").read_bytes() == csv_before
    preserved = list(local.glob("manifest.invalid-before-sync.*.bin"))
    assert len(preserved) == 1 and preserved[0].read_bytes() == broken


@pytest.mark.parametrize("remote_regresses", [False, True])
def test_interrupted_swap_restores_only_backup_and_never_backdates_arrival(environment, remote_regresses):
    mod, remote, local, args, runner = environment
    add_slot(remote, "0935")
    assert mod.pull(run=runner, **args)[0] == 0
    before = directory_bytes(local)
    local.rename(local.with_name(DAY + ".old"))
    if remote_regresses:
        (remote/DAY/"0935.csv").unlink()
        (remote/DAY/"0935.json").unlink()
        add_slot(remote, "0930")
    code, payload = mod.pull(run=runner, **args)
    assert payload["recovered_previous_directories"] == [DAY]
    assert directory_bytes(local) == before
    assert not local.with_name(DAY + ".old").exists()
    assert payload["pulled_at"] is None
    row = json.loads(args["log"].read_text(encoding="utf-8").splitlines()[-1])
    assert row["published"] is False and row["pulled_at"] is None
    if remote_regresses:
        assert code == 4 and "remote_snapshot_regressed" in payload["error"]
    else:
        assert code == 0 and row["note"] == "already_identical"


@pytest.mark.parametrize("damage", ["missing_csv", "missing_sidecar", "unknown_schema", "sidecar_array"])
def test_bad_source_records_failure_and_keeps_existing_day(environment, damage):
    mod, remote, local, args, runner = environment
    assert mod.pull(run=runner, **args)[0] == 0
    before = directory_bytes(local)
    add_slot(remote, "0935")
    if damage == "missing_csv":
        (remote/DAY/"0935.csv").unlink()
    elif damage == "missing_sidecar":
        (remote/DAY/"0935.json").unlink()
    elif damage == "sidecar_array":
        (remote/DAY/"0935.json").write_text("[]", encoding="utf-8")
    else:
        path = remote/DAY/"manifest.json"
        value = json.loads(path.read_bytes())
        value["schema_version"] = "unsupported"
        path.write_text(json.dumps(value), encoding="utf-8")
    code, payload = mod.pull(run=runner, **args)
    assert code != 0
    assert directory_bytes(local) == before and payload["pulled_at"] is None
    row = json.loads(args["log"].read_text(encoding="utf-8").splitlines()[-1])
    assert row["ok"] is False and row["published"] is False


def corrupt_transfer(raw, damage):
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        files = [(m, archive.extractfile(m).read()) for m in archive.getmembers()]
    if damage == "truncated":
        return raw[:len(raw)//2]
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for member, content in files:
            if member.name == "__snapshot_sync__.json":
                index = json.loads(content)
                if damage == "index_array":
                    index = []
                elif damage == "fingerprint_array":
                    index["files"]["manifest.json"] = []
                elif damage == "bad_hash":
                    index["files"]["manifest.json"]["sha256"] = "f"*64
                content = json.dumps(index).encode()
                member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        if damage in {"path_escape", "symlink"}:
            member = tarfile.TarInfo("../escaped.txt" if damage == "path_escape" else f"{DAY}/link")
            if damage == "symlink":
                member.type, member.linkname = tarfile.SYMTYPE, "../escaped.txt"
            archive.addfile(member, io.BytesIO(b""))
    return out.getvalue()


@pytest.mark.parametrize("damage", ["truncated", "index_array", "fingerprint_array", "bad_hash",
                                    "path_escape", "symlink"])
def test_invalid_transfer_keeps_old_files_and_writes_failure_receipt(environment, damage):
    mod, remote, local, args, runner = environment
    assert mod.pull(run=runner, **args)[0] == 0
    before = directory_bytes(local)
    add_slot(remote, "0935")

    def transport(cmd, **kwargs):
        result = runner(cmd, **kwargs)
        if cmd[-1].startswith("python3 "):
            result.stdout = corrupt_transfer(result.stdout, damage)
        return result

    code, payload = mod.pull(run=transport, **args)
    assert code != 0 and payload["pulled_at"] is None
    assert directory_bytes(local) == before
    assert not (args["tmp"]/"escaped.txt").exists()
    row = json.loads(args["log"].read_text(encoding="utf-8").splitlines()[-1])
    assert row["ok"] is False and row["stage"] == "verify" and row["published"] is False


def test_emitted_remote_program_runs_offline_and_preserves_partial_evidence(environment):
    mod, remote, local, args, runner = environment
    runner.use_emitted_program = True
    original = directory_bytes(remote/DAY)
    assert mod.pull(run=runner, **args)[0] == 0
    assert directory_bytes(local) == original
    assert json.loads((local/"manifest.json").read_bytes())["partial_slots"] == ["0930"]
    code, payload = mod.pull(run=runner, **args)
    assert code == 0 and payload["updated"] == [] and payload["pulled_at"] is None
    assert payload["transfers"][DAY]["downloaded_files"] == 0
    assert directory_bytes(remote/DAY) == original


def test_remote_listing_error_is_not_silenced(environment):
    mod, remote, local, args, runner = environment

    def listing_failure(cmd, **kwargs):
        assert cmd[-1].startswith("ls ")
        assert "|| true" not in cmd[-1] and "2>/dev/null" not in cmd[-1]
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="remote root is unavailable")

    code, payload = mod.pull(run=listing_failure, **args)
    assert code == 2 and payload["stage"] == "ls"
    assert not local.exists()
    rows = [json.loads(row) for row in args["log"].read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["ok"] is False and rows[0]["rc"] == 2
