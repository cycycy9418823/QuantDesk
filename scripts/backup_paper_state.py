"""Explicit external backup, integrity verification and non-applying restore plan."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.paper_state_backup import (
    backup_paper_state,
    build_restore_plan,
    verify_paper_state_backup,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="backup", choices=("backup", "verify", "restore-plan"))
    parser.add_argument("--source-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--learning-log", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--target-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "backup" and args.destination is None:
        parser.error("backup requires --destination outside the source checkout")
    if args.action != "backup" and args.manifest is None:
        parser.error(f"{args.action} requires --manifest")
    if args.action == "restore-plan" and args.target_root is None:
        parser.error("restore-plan requires --target-root")
    try:
        if args.action == "backup":
            result = backup_paper_state(
                args.source_root, args.destination,
                owner_learning_log=args.learning_log, dry_run=args.dry_run,
            )
            payload, ok = asdict(result), result.ok
        elif args.action == "verify":
            result = verify_paper_state_backup(args.manifest)
            payload, ok = asdict(result), result.ok
        else:
            payload = build_restore_plan(args.manifest, args.target_root)
            ok = payload["status"] == "ready_for_owner"
    except (OSError, ValueError) as exc:
        payload, ok = {"ok": False, "status": "failed", "error": f"{type(exc).__name__}:{exc}"}, False
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
