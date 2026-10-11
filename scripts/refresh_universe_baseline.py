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
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.pipeline_process_guard import market_writer_subprocess_kwargs  # noqa: E402


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
    parser.add_argument("--json", action="store_true")
    return parser


def run_refresh(
    project_root: Path,
    *,
    evidence_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
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
    backup = target_evidence / f"{universe.stem}.before_{datetime.now():%Y%m%d}.csv"
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
                today=datetime.now().date().isoformat(),
            )
        )
    if problems and not dry_run:
        shutil.copy2(backup, universe)
        after = inspect_universe(universe)
    result = {
        "schema_version": "universe_baseline_refresh_v2",
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
    }
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
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "universe-baseline-refresh: "
            f"status={'accepted' if result.get('accepted') else 'blocked'} "
            f"rows={(result.get('after') or {}).get('rows')}"
        )
    return 0 if result.get("accepted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
