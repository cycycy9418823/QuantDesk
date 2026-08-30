from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.research_daily_pipeline import (  # noqa: E402
    PipelineConfig,
    run_research_daily_pipeline,
    write_research_daily_pipeline,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the v3.8 research-only daily diagnostics pipeline. No network, no broker, no trading advice."
    )
    parser.add_argument("--data-root", default=str(PROJECT_ROOT / "data" / "daily"))
    parser.add_argument("--equity-csv", default=str(PROJECT_ROOT / "results" / "portfolio_daily_equity_rebuild_latest.csv"))
    parser.add_argument("--output", default=str(PROJECT_ROOT / "results" / "v38_daily_pipeline_latest.json"))
    parser.add_argument("--report", default=str(PROJECT_ROOT / "reports" / "v38_daily_pipeline_latest.md"))
    parser.add_argument("--limit-symbols", type=int, default=200)
    parser.add_argument("--max-dates", type=int, default=36)
    parser.add_argument("--forward-days", type=int, default=20)
    parser.add_argument("--daily-amount", type=float, default=100_000_000.0)
    parser.add_argument("--order-notional", type=float, default=100_000.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config = PipelineConfig(
        data_root=Path(args.data_root),
        equity_csv=Path(args.equity_csv),
        results_dir=Path(args.output).parent,
        reports_dir=Path(args.report).parent,
        limit_symbols=args.limit_symbols,
        max_dates=args.max_dates,
        forward_days=args.forward_days,
        daily_amount=args.daily_amount,
        order_notional=args.order_notional,
    )
    payload = run_research_daily_pipeline(config)
    files = write_research_daily_pipeline(payload, output_json=args.output, report_md=args.report)
    payload["files_written"] = [*payload.get("files_written", []), *files.values()]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(
            "v3.8 research daily pipeline "
            f"{payload.get('status')}: failed={len(payload.get('failed_steps') or [])} "
            f"warnings={len(payload.get('warning_steps') or [])}"
        )
    return 0 if payload.get("status") in {"passed", "warning"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
