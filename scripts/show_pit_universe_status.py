from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.point_in_time_universe import build_pit_status_payload, write_pit_status_outputs  # noqa: E402

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show research-only point-in-time universe status.")
    parser.add_argument("--date", default="", help="Target date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--listings-csv", default="data/universe/historical_listings.csv")
    parser.add_argument("--include-st", default="false")
    parser.add_argument("--include-suspended", default="false")
    parser.add_argument("--active-only", default="true")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    return parser.parse_args(argv)


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def run(args: argparse.Namespace) -> dict[str, Any]:
    payload = build_pit_status_payload(
        target_date=args.date or None,
        listings_csv=args.listings_csv,
        include_st=truthy(args.include_st),
        include_suspended=truthy(args.include_suspended),
        active_only=truthy(args.active_only),
    )
    payload = write_pit_status_outputs(payload, output_dir=args.output_dir)
    payload["output_paths"] = {key: _rel(value) for key, value in (payload.get("output_paths") or {}).items()}
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        summary = build_cli_summary(payload, task_type="point_in_time_universe")
        if wants_json:
            print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
        else:
            print(format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type="point_in_time_universe")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
