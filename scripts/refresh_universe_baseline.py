"""Guarded repository-owned refresh of the canonical A-share universe.

The writer is intentionally separate from the daily chain: it owns the
``a_share_symbols.csv`` freshness baseline, verifies the provider/source
contract, and restores the pre-change bytes on any post-write failure.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.pipeline_process_guard import market_writer_subprocess_kwargs  # noqa: E402
from quantdesk.full_a_daily_coverage import load_roster_evidence, prepare_roster_version  # noqa: E402
from scripts.ops.sync_snapshot_roster import send_roster  # noqa: E402

REFRESH_BUDGET_SECONDS = 35.0
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_universe(path: Path) -> dict[str, Any]:
    sources: set[str] = set()
    updated_dates: set[str] = set()
    rows = 0
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        for row in reader:
            rows += 1
            sources.add(str(row.get("source") or "").strip())
            updated_dates.add(str(row.get("updated_at") or "")[:10])
    return {
        "fields": fields,
        "rows": rows,
        "sources": sorted(sources),
        "updated_at_dates": sorted(updated_dates),
        "bytes": path.stat().st_size,
        "sha256": _sha(path),
    }


def validate_refresh(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    approved_sources: set[str],
    today: str,
) -> list[str]:
    problems: list[str] = []
    unexpected = sorted(set(after["sources"]) - approved_sources)
    if unexpected:
        problems.append(f"source_not_approved:{unexpected}")
    if before["rows"] and after["rows"] < before["rows"] * 0.95:
        problems.append(f"row_count_collapsed:{before['rows']}->{after['rows']}")
    if after["updated_at_dates"] != [today]:
        problems.append(f"updated_at_not_today:{after['updated_at_dates']}")
    if set(before["fields"]) != set(after["fields"]):
        problems.append(f"schema_changed:{before['fields']}->{after['fields']}")
    return problems


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--evidence-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--roster-sync-config", type=Path,
                        help="Optional explicit roster transport configuration; no config means no transfer")
    parser.add_argument("--json", action="store_true")
    return parser


def _replace_bytes(path: Path, raw: bytes, *, deadline: float | None = None) -> None:
    """Replace one reader-visible file after a complete, flushed write."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("roster_publication_deadline_exhausted")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_roster_version(
    root: Path, *, before: dict[str, Any], after: dict[str, Any],
    run_id: str, today: str, deadline: float, evidence_dir: Path,
) -> dict[str, Any]:
    """Publish only this run's complete, guarded response; preserve old pointers."""
    receipt_raw = (root / "data/cache/a_share_universe_status.json").read_bytes()
    receipt = json.loads(receipt_raw)
    if (receipt.get("run_id") != run_id or receipt.get("ok") is not True
            or receipt.get("full_roster_published") is not True or receipt.get("preview")
            or receipt.get("dry_run") or receipt.get("status") != "refreshed"
            or receipt.get("previous_roster_sha256") != before["sha256"]
            or receipt.get("roster_sha256") != after["sha256"]
            or receipt.get("row_count") != after["rows"]
            or after["sources"] != [receipt.get("provider_used")]):
        raise ValueError("roster_refresh_receipt_mismatch")
    universe = root / "data/universe/a_share_symbols.csv"
    raw = universe.read_bytes()
    if hashlib.sha256(raw).hexdigest() != after["sha256"]:
        raise ValueError("roster_changed_after_validation")
    with universe.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    codes = sorted(str(row.get("symbol") or "") for row in rows)
    symbols_sha = hashlib.sha256(json.dumps(codes, separators=(",", ":")).encode()).hexdigest()
    proof = next((item for item in receipt.get("source_snapshots", [])
                  if item.get("source") == receipt["provider_used"]), {})
    if (len(codes) < max(5000, receipt.get("minimum_complete_rows", 5000))
            or proof.get("status") != "complete" or proof.get("response_complete") is not True
            or proof.get("row_count") != len(codes) or proof.get("symbols") != codes
            or proof.get("symbols_sha256") != symbols_sha
            or (receipt["provider_used"] == "sina.spot_direct" and proof.get("pagination_complete") is not True)):
        raise ValueError("roster_complete_response_not_proven")
    prepared = prepare_roster_version(rows, source=receipt["provider_used"], as_of=today,
        run_id=run_id, accepted_at=datetime.now(SHANGHAI).isoformat(timespec="seconds"))
    manifest = prepared["manifest"]
    previous = load_roster_evidence(root, target_date=today)
    fallback = ({key: previous[key] for key in manifest}
                if previous.get("identity_status") == "verified_version" else manifest)
    version = root / manifest["path"]
    if not version.parent.resolve().is_relative_to(root / "data/universe"):
        raise ValueError("roster_versions_path_outside_universe")
    version.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite an immutable version. A failed publication may leave an
    # unreferenced version as evidence, but readers only follow accepted pointers.
    with version.open("xb") as handle:
        handle.write(prepared["csv_bytes"])
        handle.flush()
        os.fsync(handle.fileno())
    if _sha(version) != manifest["sha256"]:
        raise ValueError("roster_version_hash_mismatch")
    receipt_path = evidence_dir / f"{run_id}.updater.json"
    _replace_bytes(receipt_path, receipt_raw, deadline=deadline)
    pointers = [universe.parent / name for name in (
        "a_share_roster_last_accepted.json", "a_share_roster_latest.json")]
    originals = {p: p.read_bytes() if p.is_file() else None for p in pointers}
    changed: list[Path] = []
    try:
        for pointer, payload in zip(pointers, (fallback, manifest)):
            _replace_bytes(pointer, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
                           deadline=deadline)
            changed.append(pointer)
        if time.monotonic() >= deadline:
            raise TimeoutError("roster_publication_deadline_exhausted")
        verified = load_roster_evidence(root, target_date=today)
        if verified.get("sha256") != manifest["sha256"] or verified.get("fallback"):
            raise ValueError("published_roster_readback_failed")
        if time.monotonic() >= deadline:
            raise TimeoutError("roster_publication_deadline_exhausted")
    except Exception:
        for pointer in reversed(changed):
            if originals[pointer] is None:
                pointer.unlink()
            else:
                _replace_bytes(pointer, originals[pointer])
        raise
    return {"status": "published", "manifest": manifest,
            "updater_receipt_path": str(receipt_path),
            "updater_receipt_sha256": hashlib.sha256(receipt_raw).hexdigest()}


def run_refresh(
    project_root: Path,
    *,
    evidence_dir: Path | None = None,
    dry_run: bool = False,
    roster_sync_config: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + REFRESH_BUDGET_SECONDS
    run_id = "roster_" + uuid.uuid4().hex
    today = datetime.now(SHANGHAI).date().isoformat()
    root = Path(project_root).resolve()
    # Validate the active owned context before any preparation writes. Recheck
    # immediately at spawn so revoked or closed leases never reach the updater.
    market_writer_subprocess_kwargs(root)
    universe = root / "data" / "universe" / "a_share_symbols.csv"
    spec = root / "config" / "weekly_daily_wide_pool_contract.json"
    update_script = root / "scripts" / "update_a_share_universe.py"
    if not universe.is_file() or not spec.is_file() or not update_script.is_file():
        return {
            "status": "blocked",
            "accepted": False,
            "reason_codes": ["guarded_universe_input_missing"],
        }
    approved = set(
        json.loads(spec.read_text(encoding="utf-8"))["rules"][
            "approved_market_universe_sources"
        ]
    )
    target_evidence = (
        Path(evidence_dir).resolve()
        if evidence_dir is not None
        else root / "results" / "universe_baseline_refresh" / datetime.now().strftime("%Y%m%d")
    )
    target_evidence.mkdir(parents=True, exist_ok=True)
    before = inspect_universe(universe)
    backup = target_evidence / f"{universe.stem}.before_{run_id}.csv"
    if not dry_run:
        shutil.copy2(universe, backup)
        if _sha(backup) != before["sha256"]:
            return {
                "status": "blocked",
                "accepted": False,
                "reason_codes": ["pre_change_snapshot_hash_mismatch"],
            }
    command = [
        sys.executable,
        "-B",
        str(update_script),
        "--output",
        str(universe),
        "--status-path",
        str(root / "data" / "cache" / "a_share_universe_status.json"),
        "--run-id", run_id,
        # Share one absolute deadline across launch, fetch and publication.
        # The updater already reserves 2s for publication; do not charge twice
        # and shorten the primary provider's existing window.
        "--budget-seconds", str(max(0.0, deadline - time.monotonic())),
        "--deadline-monotonic", str(deadline),
    ]
    if dry_run:
        command.append("--dry-run")
    process = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        **market_writer_subprocess_kwargs(root),
    )
    tail = [line for line in process.stdout.splitlines() if line.strip()][-4:]
    problems: list[str] = []
    after = inspect_universe(universe)
    if process.returncode != 0:
        problems.append("updater_exit_nonzero")
    elif not dry_run:
        problems.extend(
            validate_refresh(
                before=before,
                after=after,
                approved_sources=approved,
                today=today,
            )
        )
    publication: dict[str, Any] = {"status": "dry_run" if dry_run else "not_published"}
    if not problems and not dry_run:
        try:
            publication = _publish_roster_version(root, before=before, after=after,
                run_id=run_id, today=today, deadline=deadline, evidence_dir=target_evidence)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            problems.append("roster_version_publication_failed:" + type(exc).__name__ + ":" + str(exc))
    if problems and not dry_run:
        _replace_bytes(universe, backup.read_bytes())
        after = inspect_universe(universe)
    result = {
        "schema_version": "universe_baseline_refresh_v2",
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_file": str(universe),
        "approved_sources": sorted(approved),
        "before": before,
        "after": after,
        "pre_change_snapshot": str(backup) if not dry_run else "",
        "updater_exit": process.returncode,
        "updater_tail": tail,
        "problems": problems,
        "accepted": not problems and process.returncode == 0,
        "dry_run": dry_run,
        "restored_after_failure": bool(problems and not dry_run),
        "roster_publication": publication,
        "budget_seconds": REFRESH_BUDGET_SECONDS,
        "duration_seconds": max(0.0, time.monotonic() - started),
    }
    # Publication retains its original 35-second budget. Transport has a
    # separate bounded budget and cannot turn local success into cloud success.
    result["roster_sync"] = (send_roster(root, config_path=roster_sync_config,
        expected_run_id=run_id) if publication.get("status") == "published"
        else {"status": "dry_run" if dry_run else "local_roster_not_published",
              "network_attempted": False, "reason_codes": []})
    result["total_duration_seconds"] = max(0.0, time.monotonic() - started)
    if not dry_run:
        (target_evidence / "universe_baseline_refresh.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = run_refresh(
        Path(args.project_root),
        evidence_dir=Path(args.evidence_dir) if args.evidence_dir else None,
        dry_run=bool(args.dry_run),
        roster_sync_config=args.roster_sync_config,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "universe-baseline-refresh: "
            f"status={'accepted' if result.get('accepted') else 'blocked'} "
            f"rows={(result.get('after') or {}).get('rows')} "
            f"roster_sync={(result.get('roster_sync') or {}).get('status', 'not_started')}"
        )
    return 0 if result.get("accepted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
