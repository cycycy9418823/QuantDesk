"""Explicit offline pack, verify and detached restore of paper evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.paper_state_backup import pack_backup_evidence, verify_backup_archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--source-root", help="Explicit detached source for pack; omitted for verification")
    parser.add_argument("--reference-origin-root", help="Original root recorded in frozen references; only detached bytes are read")
    parser.add_argument("--isolated-restore-root", help="New external directory only; never restores production")
    args = parser.parse_args(argv)
    result = (pack_backup_evidence(args.source_root, args.archive, reference_origin_root=args.reference_origin_root) if args.source_root else
        verify_backup_archive(args.archive, isolated_restore_root=args.isolated_restore_root))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
