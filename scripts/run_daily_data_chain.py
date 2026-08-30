from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.daily_data_chain import run_daily_data_chain
from quantdesk.shared import truthy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the authoritative QuantDesk daily data chain."
    )
    parser.add_argument("--target-date", default="")
    parser.add_argument("--network", default="false")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--dry-run", default="false")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--batch-workers", type=int, default=4)
    parser.add_argument("--retry-from-latest", default="true")
    parser.add_argument("--skip-analysis-pool-minute", default="false")
    parser.add_argument("--rebuild-features", default="true")
    parser.add_argument("--refresh-universe", default="true")
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--json", default="true")
    parser.add_argument("--summary", default="false")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] or None
    payload = run_daily_data_chain(
        project_root=PROJECT_ROOT,
        target_date=args.target_date or None,
        symbols=symbols,
        network=bool(args.enable_network or truthy(args.network)),
        dry_run=truthy(args.dry_run),
        batch_size=max(1, int(args.batch_size)),
        batch_workers=max(1, min(int(args.batch_workers), 8)),
        rebuild_features=truthy(args.rebuild_features),
        retry_from_latest=truthy(args.retry_from_latest),
        skip_analysis_pool_minute=truthy(args.skip_analysis_pool_minute),
        refresh_universe=truthy(args.refresh_universe),
        trigger=args.trigger,
    )
    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=None if truthy(args.summary) else 2))
    else:
        coverage = payload.get("coverage") or {}
        print(
            f"daily-data-chain status={payload.get('status')} "
            f"target={payload.get('target_date')} run_id={payload.get('run_id')}"
        )
        print(
            "daily={daily} critical={critical} analysis={analysis}".format(
                daily=(coverage.get("daily") or {}).get("coverage_ratio"),
                critical=(coverage.get("critical") or {}).get("coverage_ratio"),
                analysis=(coverage.get("current_analysis_pool") or {}).get("coverage_ratio"),
            )
        )
        print(payload.get("next_action") or "")
    status = str(payload.get("status") or "")
    if status in {"completed", "dry_run", "skipped_already_completed"}:
        return 0
    if status == "partial":
        return 2
    if status == "busy":
        return 3
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
