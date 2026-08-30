"""Collect offline Stage 4 forward-window maintenance evidence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_PROJECT_ROOT))

from quantdesk.paper.stage4_final_evaluator import (  # noqa: E402
    build_stage4_final_evaluation_artifact,
    write_stage4_final_evaluation_artifact,
)
from quantdesk.paper.stage4_maintenance_evidence import (  # noqa: E402
    collect_stage4_maintenance_evidence,
    write_stage4_maintenance_evidence,
)
from quantdesk.shared import truthy  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(DEFAULT_PROJECT_ROOT))
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--readiness-path", default="")
    parser.add_argument("--t0-birth-path", default="")
    parser.add_argument("--chain-runs-dir", default="")
    parser.add_argument("--shadow-archive-dir", default="")
    parser.add_argument("--shadow-latest-path", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--json", dest="as_json", default="false")
    parser.add_argument("--summary", default="true")
    parser.add_argument("--network", default="false")
    return parser.parse_args(argv)


def _optional_path(value: str) -> Path | None:
    return Path(value) if str(value or "").strip() else None


def _results_output_dir(root: Path, value: str) -> Path | None:
    project_root = root.resolve()
    results_root = (project_root / "results").resolve()
    try:
        results_root.relative_to(project_root)
    except ValueError:
        return None
    target = Path(value or results_root / "data_health").resolve()
    try:
        target.relative_to(results_root)
    except ValueError:
        return None
    return target


def _fixed_terminal_date(root: Path, readiness_path: Path | None, requested: str) -> str:
    """Pin post-terminal retries to the finite T0+18 calendar date."""

    readiness_file = Path(
        readiness_path
        or root / "results" / "data_health" / "round7_forward_pit_readiness_latest.json"
    )
    try:
        payload = json.loads(readiness_file.read_text(encoding="utf-8-sig"))
        milestones = payload.get("milestones") if isinstance(payload, dict) else None
        terminal = next(
            str(item.get("calendar_date") or "")[:10]
            for item in milestones
            if isinstance(item, dict)
            and str(item.get("relative_to_t0") or "").startswith("T0+18")
        )
        if terminal and str(requested)[:10] >= terminal:
            return terminal
    except (OSError, UnicodeError, json.JSONDecodeError, StopIteration, TypeError):
        pass
    return str(requested)[:10]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if truthy(args.network):
        print(
            "stage4-maintenance is offline-only; --network must be false",
            file=sys.stderr,
        )
        return 2
    root = Path(args.project_root).resolve()
    effective_as_of = _fixed_terminal_date(
        root, _optional_path(args.readiness_path), str(args.as_of)
    )
    output_dir = _results_output_dir(root, str(args.output_dir))
    if output_dir is None:
        print(
            "stage4-maintenance --output-dir must stay under PROJECT_ROOT/results",
            file=sys.stderr,
        )
        return 2
    payload = collect_stage4_maintenance_evidence(
        root,
        as_of=effective_as_of,
        readiness_path=_optional_path(args.readiness_path),
        t0_birth_path=_optional_path(args.t0_birth_path),
        chain_runs_dir=_optional_path(args.chain_runs_dir),
        shadow_archive_dir=_optional_path(args.shadow_archive_dir),
        shadow_latest_path=_optional_path(args.shadow_latest_path),
    )
    paths = write_stage4_maintenance_evidence(
        payload,
        output_dir=output_dir,
        results_root=root / "results",
        as_of=effective_as_of,
    )
    final_paths = None
    if payload.get("terminal_reached") is True:
        adapter_path = (
            root
            / "results"
            / "stage4_replay_adapter"
            / effective_as_of
            / "adapter_result.json"
        )
        try:
            final_payload = build_stage4_final_evaluation_artifact(
                adapter_path=adapter_path,
                project_root=root,
                maintenance_evidence=payload,
                as_of=effective_as_of,
            )
            final_paths = write_stage4_final_evaluation_artifact(
                final_payload,
                output_dir=output_dir,
                results_root=root / "results",
                as_of=effective_as_of,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(
                f"stage4-maintenance final evaluation failed: {type(exc).__name__}:{exc}",
                file=sys.stderr,
            )
            return 78
    if truthy(args.as_json):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif truthy(args.summary):
        coverage = payload["preregistered_checks"]["coverage_continuity"]
        counts = payload["preregistered_checks"]["daily_count_integrity"]
        print(
            "stage4-maintenance: "
            f"status={payload['status']} t0={payload.get('t0')} "
            f"eligible_days={payload['eligible_forward_day_count']} "
            f"coverage={coverage['complete_day_count']}/{coverage['expected_day_count']} "
            f"shadow_days={counts['observed_day_count']} -> {paths['latest']}"
        )
        if final_paths is not None:
            print(f"  final_evaluation={final_paths['latest']}")
        print(
            f"  account_replay={payload.get('account_replay')} / "
            "conclusion_drawn=false / Research Only / Paper Account Only / "
            "No Broker Route."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
