"""Run a bounded bootstrap research job in an explicit external persistent directory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.research.resumable_bootstrap_job import (  # noqa: E402
    DEFAULT_ITERATIONS, DEFAULT_SEED, JobBusy, JobError, run_job,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-root", type=Path, required=True, help="External directory that must survive for resume.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input", dest="input_path", type=Path, help="CSV with return column; required only for creation.")
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    try:
        result = run_job(**vars(args))
    except (JobError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 3 if isinstance(exc, JobBusy) else 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
