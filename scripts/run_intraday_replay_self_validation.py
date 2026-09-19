from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.intraday_replay_self_validation import run_intraday_replay_self_validation
from quantdesk.shared import truthy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic intraday replay self-validation with synthetic data.")
    parser.add_argument("--keep-artifacts", default="true")
    parser.add_argument("--write-latest", action="store_true")
    parser.add_argument("--update-portfolio", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_intraday_replay_self_validation(
        keep_artifacts=truthy(args.keep_artifacts),
        write_latest=bool(args.write_latest),
        update_portfolio=bool(args.update_portfolio),
        verbose=bool(args.verbose),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
