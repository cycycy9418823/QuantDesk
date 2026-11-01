"""Opt-in accepted-roster transfer; never fetch quotes or install remote code.

The dedicated config/snapshot_roster_sync.json references the existing backup
transport config and explicitly names the receiver script. Missing configuration
or network != true means no transfer. The receiver needs an explicit --apply.
"""
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Callable
import uuid
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.full_a_daily_coverage import load_roster_evidence, prepare_roster_version  # noqa: E402
from quantdesk.interprocess_lock import interprocess_file_lock  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")
SYNC_BUDGET_SECONDS = 45.0
MAX_PACKET_BYTES = 12 * 1024 * 1024
MAX_CSV_BYTES = 8 * 1024 * 1024
COLLECTOR_LOCK = Path("data/snapshots/.locks/collector.lock")
POINTERS = ("a_share_roster_last_accepted.json", "a_share_roster_latest.json")
MANIFEST_KEYS = ("schema", "status", "accepted", "source", "as_of", "accepted_at",
                 "run_id", "path", "row_count", "sha256", "symbols_sha256")


class _RollbackIncomplete(OSError):
    """The receiver cannot confirm that all original pointers were restored."""


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _moment(now: datetime | None) -> datetime:
    moment = now or datetime.now(SHANGHAI)
    if moment.tzinfo is None:
        raise ValueError("timezone_required")
    return moment.astimezone(SHANGHAI)


def _decode_packet(packet: bytes, now: datetime) -> tuple[dict[str, Any], bytes]:
    if len(packet) > MAX_PACKET_BYTES:
        raise ValueError("roster_packet_too_large")
    envelope = json.loads(packet)
    if (not isinstance(envelope, dict) or set(envelope) != {
            "schema", "request_id", "expires_at", "manifest", "csv_base64"}
            or envelope["schema"] != "snapshot_roster_transfer.v1"
            or uuid.UUID(envelope["request_id"]).hex != envelope["request_id"]):
        raise ValueError("roster_packet_schema_invalid")
    expires = datetime.fromisoformat(envelope["expires_at"])
    if expires.tzinfo is None or not 0 < (expires - now).total_seconds() <= 60:
        raise ValueError("roster_packet_expired_or_clock_mismatch")
    raw = base64.b64decode(envelope["csv_base64"], validate=True)
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("roster_csv_too_large")
    manifest = envelope["manifest"]
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    prepared = prepare_roster_version(rows, source=manifest["source"], as_of=manifest["as_of"],
        run_id=manifest["run_id"], accepted_at=manifest["accepted_at"])
    if manifest != prepared["manifest"] or raw != prepared["csv_bytes"]:
        raise ValueError("roster_packet_identity_mismatch")
    if manifest["row_count"] < 5000:
        raise ValueError("roster_incomplete")
    if (manifest["as_of"] > now.date().isoformat()
            or datetime.fromisoformat(manifest["accepted_at"]) > now + timedelta(seconds=5)):
        raise ValueError("roster_from_future")
    return envelope, raw


def build_roster_packet(project_root: Path, *, now: datetime | None = None,
                       expected_run_id: str | None = None) -> bytes:
    """Read one accepted version, retaining its original date and identity."""
    moment = _moment(now)
    root = Path(project_root).resolve()
    evidence = load_roster_evidence(root, target_date=moment.date().isoformat())
    if evidence.get("identity_status") != "verified_version" or evidence.get("fallback"):
        raise ValueError("latest_accepted_roster_unavailable")
    manifest = {key: evidence[key] for key in MANIFEST_KEYS}
    if expected_run_id is not None and manifest["run_id"] != expected_run_id:
        raise ValueError("published_roster_changed_before_transfer")
    with (root / manifest["path"]).open("rb") as handle:
        raw = handle.read(MAX_CSV_BYTES + 1)
    packet = _json_bytes({"schema": "snapshot_roster_transfer.v1", "request_id": uuid.uuid4().hex,
        "expires_at": (moment + timedelta(seconds=SYNC_BUDGET_SECONDS - 5)).isoformat(),
        "manifest": manifest, "csv_base64": base64.b64encode(raw).decode("ascii")})
    _decode_packet(packet, moment)
    return packet


def _owned_path(root: Path, relative: str | Path) -> Path:
    target = root / relative
    if not target.resolve().is_relative_to(root):
        raise ValueError("roster_target_outside_project")
    for part in (target, *target.parents):
        if part == root:
            break
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("roster_target_link_rejected")
    return target


def _transport_config(path: Path) -> dict[str, Any]:
    from quantdesk.paper_state_backup import backup_transport_config

    with path.open("rb") as handle:
        raw = handle.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("roster_sync_config_too_large")
    settings = json.loads(raw)
    if (not isinstance(settings, dict) or set(settings) - {"network", "transport_config", "receiver_script"}
            or not isinstance(settings.get("network"), bool)):
        raise ValueError("roster_sync_config_invalid")
    if settings["network"] is not True:
        return {"network": False}
    script = settings["receiver_script"]
    if (not isinstance(script, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", script)
            or ".." in PurePosixPath(script).parts or PurePosixPath(script).name != "sync_snapshot_roster.py"):
        raise ValueError("roster_sync_receiver_path_invalid")
    transport = Path(settings["transport_config"])
    config = backup_transport_config(transport if transport.is_absolute() else path.parent / transport)
    return {**config, "receiver_script": script}


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("roster_receive_deadline_exhausted")


def _replace_bytes(path: Path, raw: bytes, deadline: float | None = None) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if deadline is not None:
            _check_deadline(deadline)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _previous_manifest(current: dict[str, Any]) -> dict[str, Any] | None:
    if current.get("identity_status") == "verified_version":
        return {key: current[key] for key in MANIFEST_KEYS}
    return None


def _check_order(current: dict[str, Any], manifest: dict[str, Any]) -> bool:
    previous = _previous_manifest(current)
    if previous == manifest:
        return bool(current.get("fallback"))
    old_day = current.get("as_of")
    if current.get("row_count", 0) and not old_day:
        raise ValueError("existing_roster_date_unknown")
    if old_day and manifest["as_of"] < old_day:
        raise ValueError("older_roster_rejected")
    if (previous and manifest["as_of"] == old_day
            and datetime.fromisoformat(manifest["accepted_at"]) <= datetime.fromisoformat(previous["accepted_at"])):
        raise ValueError("non_newer_roster_rejected")
    if manifest["row_count"] < current.get("row_count", 0) * 0.95:
        raise ValueError("roster_count_collapsed")
    return True


def _activate(root: Path, envelope: dict[str, Any], raw: bytes, now: datetime,
              deadline: float) -> dict[str, Any]:
    manifest = envelope["manifest"]
    current = load_roster_evidence(root, target_date=now.date().isoformat())
    if not _check_order(current, manifest):
        return {"status": "already_current", "changed": False, "manifest": _previous_manifest(current)}
    version = _owned_path(root, manifest["path"])
    pointers = [_owned_path(root, Path("data/universe") / name) for name in POINTERS]
    version.parent.mkdir(parents=True, exist_ok=True)
    _check_deadline(deadline)
    if version.exists():
        if version.read_bytes() != raw:
            raise ValueError("immutable_roster_version_collision")
    else:
        # The shared collector lock serializes receivers. Never replace an
        # existing version, and retain an unreferenced preparation on failure.
        with version.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    if hashlib.sha256(version.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("received_roster_readback_mismatch")
    before = {path: path.read_bytes() if path.is_file() else None for path in pointers}
    changed: list[Path] = []
    try:
        for path, value in zip(pointers, (_previous_manifest(current) or manifest, manifest)):
            _replace_bytes(path, _json_bytes(value), deadline)
            changed.append(path)
        actual = load_roster_evidence(root, target_date=now.date().isoformat())
        if actual.get("fallback") or _previous_manifest(actual) != manifest:
            raise ValueError("activated_roster_readback_mismatch")
        _check_deadline(deadline)
    except Exception as failure:
        restore_errors = []
        for path in reversed(changed):
            try:
                if before[path] is None:
                    path.unlink()
                else:
                    _replace_bytes(path, before[path])
            except OSError as exc:
                restore_errors.append(exc)
        if restore_errors:
            raise _RollbackIncomplete("roster_rollback_incomplete") from failure
        raise
    return {"status": "applied", "changed": True, "manifest": _previous_manifest(actual)}


def receive_roster_packet(project_root: Path, packet: bytes, *, apply: bool = False,
                          now: datetime | None = None) -> dict[str, Any]:
    """Verify the whole packet, then switch pointers under the collector's lock."""
    result: dict[str, Any] = {"schema": "snapshot_roster_receipt.v1", "status": "rejected",
        "changed": False, "request_id": "", "manifest": None, "reason_codes": []}
    try:
        moment = _moment(now)
        envelope, raw = _decode_packet(packet, moment)
        result.update(request_id=envelope["request_id"], received_at=moment.isoformat())
        root = Path(project_root).resolve()
        lock = _owned_path(root, COLLECTOR_LOCK)
        _owned_path(root, "data/universe/versions")
        for name in POINTERS:
            _owned_path(root, Path("data/universe") / name)
        remaining = (datetime.fromisoformat(envelope["expires_at"]) - moment).total_seconds()
        deadline = time.monotonic() + min(10.0, remaining)
        if not apply:
            current = load_roster_evidence(root, target_date=moment.date().isoformat())
            _check_order(current, envelope["manifest"])
            return {**result, "status": "dry_run", "manifest": envelope["manifest"]}
        with interprocess_file_lock(lock, timeout_seconds=0):
            result.update(_activate(root, envelope, raw, moment, deadline))
    except _RollbackIncomplete:
        result.update(status="unknown", changed=None, reason_codes=["roster_rollback_incomplete"])
    except TimeoutError as exc:
        busy = str(exc).startswith("interprocess_file_lock_timeout:")
        result.update(status="busy" if busy else "failed", reason_codes=[
            "collector_lock_busy" if busy else "roster_receive_deadline_exhausted"])
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, csv.Error) as exc:
        result["reason_codes"] = [str(exc) if isinstance(exc, ValueError) else "roster_packet_invalid:" + type(exc).__name__]
    except OSError as exc:
        result.update(status="failed", reason_codes=["roster_receive_io_error:" + type(exc).__name__])
    return result


def send_roster(project_root: Path, *, config_path: Path | None = None, dry_run: bool = False,
                expected_run_id: str | None = None, now: datetime | None = None,
                run: Callable[..., subprocess.CompletedProcess] | None = None) -> dict[str, Any]:
    """Send via one bounded SSH child; success requires a matching remote receipt."""
    started = time.monotonic()
    root = Path(project_root).resolve()
    config_file = Path(config_path) if config_path is not None else root / "config/snapshot_roster_sync.json"
    result: dict[str, Any] = {"status": "not_configured", "network_attempted": False,
        "reason_codes": [], "budget_seconds": SYNC_BUDGET_SECONDS}
    if config_path is None and not config_file.is_file():
        return result
    try:
        config = _transport_config(config_file)
        if dry_run or config.get("network") is not True:
            return {**result, "status": "dry_run" if dry_run else "network_disabled"}
        if config.get("project_root") and Path(config["project_root"]).resolve() != root:
            raise ValueError("roster_sync_project_mismatch")
        remote_snaps = PurePosixPath(config["remote_snapshot_root"])
        if remote_snaps.name != "snapshots" or remote_snaps.parent.name != "data":
            raise ValueError("roster_sync_remote_snapshot_path_invalid")
        remote_root = remote_snaps.parent.parent
        if str(remote_root) == "/":
            raise ValueError("roster_sync_remote_root_invalid")
        key, known_hosts = Path(config["ssh_key_path"]), Path(config["known_hosts_path"])
        if not key.is_file() or not known_hosts.is_file():
            raise ValueError("roster_sync_ssh_identity_files_missing")
        packet = build_roster_packet(root, now=now, expected_run_id=expected_run_id)
        envelope = json.loads(packet)
        command = ["ssh", "-T", "-i", str(key), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]
        if sys.platform == "win32":
            # Reuse the existing bounded physical-route helper with this
            # configuration's host, rather than the backup script's globals.
            proxy = subprocess.list2cmdline([sys.executable, "-B",
                str(Path(__file__).with_name("physical_ssh_transport.py")),
                config["remote"].rsplit("@", 1)[-1], "22"])
            command += ["-o", "ProxyCommand=" + proxy]
        command += ["--", config["remote"], shlex.join(["python3", "-B",
            config["receiver_script"], "receive",
            "--project-root", str(remote_root), "--apply"])]
        remaining = SYNC_BUDGET_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("roster_send_preparation_deadline_exhausted")
        result["network_attempted"] = True
        process = (run or subprocess.run)(command, input=packet, capture_output=True,
            timeout=remaining, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if len(process.stdout) > 64 * 1024:
            raise ValueError("roster_sync_receipt_too_large")
        receipt = json.loads(process.stdout)
        if (receipt.get("schema") != "snapshot_roster_receipt.v1"
                or receipt.get("request_id") != envelope["request_id"]):
            raise ValueError("roster_sync_receipt_identity_mismatch")
        result["remote_receipt"] = receipt
        if process.returncode != 0 or receipt.get("status") not in {"applied", "already_current"}:
            return {**result, "status": "unknown" if receipt.get("status") == "unknown" else "failed",
                    "reason_codes": ["remote_roster_not_accepted"]}
        if receipt.get("manifest") != envelope["manifest"]:
            raise ValueError("roster_sync_receipt_manifest_mismatch")
        result.update(status="synced", duration_seconds=max(0.0, time.monotonic() - started))
    except (subprocess.TimeoutExpired, TimeoutError):
        result.update(status="unknown" if result["network_attempted"] else "failed",
            reason_codes=["roster_sync_timeout_remote_state_unknown" if result["network_attempted"]
                          else "roster_sync_preparation_timeout"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result.update(status="unknown" if result["network_attempted"] else "failed",
            reason_codes=["roster_sync_error:" + type(exc).__name__])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("send", "receive"))
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Explicitly allow receiver pointer publication")
    args = parser.parse_args(argv)
    if args.action == "receive":
        result = receive_roster_packet(args.project_root, sys.stdin.buffer.read(MAX_PACKET_BYTES + 1),
            apply=args.apply and not args.dry_run)
    else:
        result = send_roster(args.project_root, config_path=args.config, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"synced", "applied", "already_current", "dry_run",
                                    "not_configured", "network_disabled"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
