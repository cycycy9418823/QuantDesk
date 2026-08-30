from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.factor_research_dashboard import (  # noqa: E402
    build_factor_research_dashboard,
    build_synthetic_research_payloads,
    load_latest_research_payloads,
    write_research_dashboard_outputs,
)

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a research-only QuantDesk factor research dashboard.")
    parser.add_argument("--network", default="false", help="Defaults to false. This command never fetches market data.")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--stale-hours", type=int, default=48)
    parser.add_argument("--factor-health-json", default="results/factor_health_latest.json")
    parser.add_argument("--combined-score-json", default="results/combined_score_latest.json")
    parser.add_argument("--combined-score-backtest-json", default="results/combined_score_backtest_latest.json")
    parser.add_argument("--sector-rotation-json", default="results/sector_rotation_latest.json")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--synthetic-test-data", default="false", help="Test-only synthetic payloads; never used by default.")
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
    warnings: list[str] = []
    if truthy(args.network):
        warnings.append("network_requested_ignored: research-dashboard is offline and does not fetch market data")
    if truthy(args.synthetic_test_data):
        loaded = build_synthetic_research_payloads()
        synthetic = True
    else:
        loaded = load_latest_research_payloads({
            "inputs": {
                "factor_health_json": args.factor_health_json,
                "combined_score_json": args.combined_score_json,
                "combined_score_backtest_json": args.combined_score_backtest_json,
                "sector_rotation_json": args.sector_rotation_json,
            }
        })
        synthetic = False
    payload = build_factor_research_dashboard(loaded, stale_hours=args.stale_hours, synthetic_payloads=synthetic)
    payload["network"] = False
    payload["warnings"] = list(dict.fromkeys(str(item) for item in list(payload.get("warnings") or []) + warnings if item))
    if payload["warnings"]:
        payload["status"] = "warning" if payload.get("status") != "failed" else "failed"
    payload = write_research_dashboard_outputs(payload, output_dir=args.output_dir, report_dir=args.report_dir)
    payload["output_paths"] = {key: _rel(value) for key, value in (payload.get("output_paths") or {}).items()}
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        summary = build_cli_summary(payload, task_type="factor_research_dashboard")
        if wants_json:
            print(json.dumps(summary, ensure_ascii=True, separators=(",", ":")))
        else:
            print(format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type="factor_research_dashboard")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
