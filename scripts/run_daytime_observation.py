"""Explicitly date-bounded AI observation. No paper ledger or queue writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-observation", action="store_true")
    parser.add_argument("--authorized-date", default="", help="Only this YYYY-MM-DD; never rolls to another day.")
    args = parser.parse_args(argv)
    from ai_trader.daytime_observation import INPUT_WAIT_SECONDS, run_observation, write_daily_summary
    result = run_observation(PROJECT_ROOT, enable=args.enable_observation, authorized_date=args.authorized_date,
                             wait_seconds=INPUT_WAIT_SECONDS, enable_local_quotes=True)
    if result.get("receipt"):
        try:
            result["summary"] = str(write_daily_summary(PROJECT_ROOT, args.authorized_date))
        except (OSError, RuntimeError, ValueError) as exc:
            result["summary_error"] = type(exc).__name__
    # Raw model text and account context stay in the private observation receipt.
    print(json.dumps({k: result.get(k) for k in (
        "status", "reason", "receipt", "calls_this_run", "written", "observation_only",
        "ledger_written", "execution_queue_written", "evidence_status", "summary", "summary_error")}, ensure_ascii=False))
    return 1 if result.get("summary_error") or result["status"] in {"model_failed", "save_failed", "blocked", "busy"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
