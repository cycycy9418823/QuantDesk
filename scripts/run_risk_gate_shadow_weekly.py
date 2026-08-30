"""Build the offline five-observation risk-gate shadow weekly review."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.risk.risk_gate_shadow_weekly import (  # noqa: E402
    MIN_OBSERVED_DAYS,
    load_shadow_daily_window,
    render_weekly_markdown,
    summarize_shadow_weekly,
    write_weekly_report,
)
from quantdesk.shared import truthy  # noqa: E402
from quantdesk.utils import atomic_write_json  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument(
        "--archive-dir",
        default="",
    )
    parser.add_argument(
        "--latest-path",
        default="",
    )
    parser.add_argument(
        "--reports-dir",
        default="",
    )
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--json", dest="as_json", default="false")
    parser.add_argument("--summary", default="true")
    parser.add_argument("--network", default="false")
    return parser.parse_args(argv)


def _reports_output_dir(root: Path, value: str) -> Path | None:
    project_root = root.resolve()
    reports_root = (project_root / "reports").resolve()
    try:
        reports_root.relative_to(project_root)
    except ValueError:
        return None
    target = Path(value or reports_root / "risk_gate_shadow").resolve()
    try:
        target.relative_to(reports_root)
    except ValueError:
        return None
    return target


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if truthy(args.network):
        print("shadow-weekly is offline-only; --network must be false", file=sys.stderr)
        return 2
    root = Path(args.project_root).resolve()
    reports_dir = _reports_output_dir(root, str(args.reports_dir))
    if reports_dir is None:
        print(
            "shadow-weekly --reports-dir must stay under PROJECT_ROOT/reports",
            file=sys.stderr,
        )
        return 2
    window = load_shadow_daily_window(
        archive_dir=Path(
            args.archive_dir or root / "results" / "risk_gate_shadow_daily"
        ),
        latest_path=Path(
            args.latest_path or root / "results" / "risk_gate_shadow_latest.json"
        ),
        as_of=str(args.as_of),
        limit=MIN_OBSERVED_DAYS,
    )
    review = summarize_shadow_weekly(window.summaries, as_of=str(args.as_of))
    review.input_rejections = list(window.rejected)
    paths = write_weekly_report(
        review,
        reports_dir=reports_dir,
        reports_root=root / "reports",
        as_of=str(args.as_of),
    )

    dated_payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    latest_json = reports_dir / "risk_gate_shadow_weekly_latest.json"
    latest_markdown = reports_dir / "risk_gate_shadow_weekly_latest.md"
    atomic_write_json(latest_json, dated_payload)
    latest_markdown.write_text(render_weekly_markdown(review), encoding="utf-8")

    if truthy(args.as_json):
        print(json.dumps(dated_payload, ensure_ascii=False, indent=2))
    elif truthy(args.summary):
        print(
            "shadow-weekly: "
            f"mode={review.mode} observed_days={review.observed_days}/"
            f"{MIN_OBSERVED_DAYS} rejected={len(window.rejected)} "
            f"-> {latest_json}"
        )
        print(
            "  Research Only / Paper Account Only / No Broker Route / "
            "Live Execution Locked / Not Investment Advice."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
