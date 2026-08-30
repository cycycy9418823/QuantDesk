from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk.update_progress_reporter import build_coverage_status, format_coverage_status


def main() -> int:
    parser = argparse.ArgumentParser(description="Show v4.2.6 update coverage status.")
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    payload = build_coverage_status(Path(args.root).resolve())
    print(format_coverage_status(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
