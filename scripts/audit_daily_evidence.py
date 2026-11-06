"""Read-only audit of declared daily research inputs and a saved backup."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.daily_evidence_contract import audit_daily_evidence, load_ref, read_object, readiness_note  # noqa: E402
from quantdesk.paper_state_backup import verify_backup_archive  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--use-sealed-requirements", action="store_true",
                        help="Use the dated immutable checklist for a recovery audit.")
    parser.add_argument("--backup-archive", type=Path)
    parser.add_argument("--network", choices=("false",), default="false")
    parser.add_argument("--json", choices=("true", "false"), default="true")
    args = parser.parse_args(argv)
    try:
        root = args.project_root.resolve()
        requirements = None
        if args.use_sealed_requirements:
            pointer = read_object(root, Path("results/daily_evidence") / args.target_date / "latest.json")
            recorded = json.loads(load_ref(root, pointer))
            requirements = recorded["requirements"]
        result = audit_daily_evidence(root, args.target_date, requirements=requirements)
        result["note"] = readiness_note(result)
        if args.backup_archive:
            result["backup"] = verify_backup_archive(args.backup_archive, target_date=args.target_date)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        result = {"schema": "daily_evidence_audit.v1", "status": "not_assessed", "target_date": args.target_date,
                  "reason_codes": [f"evidence_audit_failed:{type(exc).__name__}"], "network_used": False, "runtime_written": False}
        print(json.dumps(result, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json == "true" else result["note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
