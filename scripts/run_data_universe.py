from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.shared import truthy  # noqa: E402
from scripts.build_historical_listings import build_historical_listings  # noqa: E402
from scripts.build_historical_listings import write_outputs as write_build_outputs
from scripts.validate_historical_listings import validate_historical_listings  # noqa: E402
from scripts.validate_historical_listings import write_outputs as write_validate_outputs


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or validate runtime PIT universe files.")
    sub = parser.add_subparsers(dest="action")
    build = sub.add_parser("build-listings")
    build.add_argument("--data-root", default="data")
    build.add_argument("--output", default="data/universe/historical_listings.csv")
    build.add_argument("--network", default="false")
    build.add_argument("--write", default="true")
    build.add_argument("--json", default="false")
    build.add_argument("--summary", nargs="?", const="true", default="false")
    validate = sub.add_parser("validate-listings")
    validate.add_argument("--path", "--csv", dest="path", default="data/universe/historical_listings.csv")
    validate.add_argument("--json", default="false")
    validate.add_argument("--summary", nargs="?", const="true", default="false")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.action == "build-listings":
        payload = write_build_outputs(build_historical_listings(
            data_root=args.data_root,
            output_path=args.output,
            write=truthy(args.write),
            network=truthy(args.network),
        ))
        wants_json = truthy(args.json) or truthy(args.summary)
    elif args.action == "validate-listings":
        payload = write_validate_outputs(validate_historical_listings(args.path))
        wants_json = truthy(args.json) or truthy(args.summary)
    else:
        payload = {"ok": False, "status": "failed", "task_type": "data_universe", "errors": ["missing_action"]}
        wants_json = True
    if wants_json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
