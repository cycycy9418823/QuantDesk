from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.research.risk_stat_validation import build_risk_stat_validation, write_risk_stat_validation  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build research-only risk/stat validation from local equity CSV.")
    parser.add_argument("--equity-csv", default=str(PROJECT_ROOT / "results" / "portfolio_daily_equity_rebuild_latest.csv"))
    parser.add_argument("--portfolio-name", default="combo_50_50")
    parser.add_argument("--order-notional", type=float, default=100_000.0)
    parser.add_argument("--daily-amount", type=float, default=0.0)
    parser.add_argument("--output", default=str(PROJECT_ROOT / "results" / "risk_stat_validation_latest.json"))
    parser.add_argument("--report", default=str(PROJECT_ROOT / "reports" / "risk_stat_validation_latest.md"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = build_risk_stat_validation(
        args.equity_csv,
        portfolio_name=args.portfolio_name,
        order_notional=args.order_notional,
        daily_amount=args.daily_amount,
    )
    files = write_risk_stat_validation(payload, args.output, args.report)
    payload["files_written"] = list(files.values())
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        metrics = payload.get("metrics") or {}
        print(
            "risk/stat validation "
            f"{payload.get('status')}: sharpe={metrics.get('sharpe')} mdd={metrics.get('max_drawdown')}"
        )
    return 0 if payload.get("status") in {"passed", "warning"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
