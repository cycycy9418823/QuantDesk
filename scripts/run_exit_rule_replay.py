"""Read existing local prices and write an explicit, labelled exit comparison.

No network, model, ledger or strategy writes.
Example: python scripts/run_exit_rule_replay.py --as-of 2026-11-02 --output-dir D:/research
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CANON = Path(__file__).resolve().parents[1]
if str(CANON) not in sys.path:
    sys.path.insert(0, str(CANON))

from quantdesk.research import exit_rule_replay as replay  # noqa: E402

# Compatibility for callers that previously imported the CLI helpers.
build_report = replay.build_report
conclusion = replay.conclusion


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(CANON))
    parser.add_argument("--as-of", default="")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        index = replay.load_index_bars(root / "data/benchmark/hs300.csv")
        as_of = replay.parse_day(args.as_of) if args.as_of else index[-1].day
        output = Path(args.output_dir) if args.output_dir else root / "reports/research"
        result = replay.write_report(root, as_of, output)
    except (replay.EvidenceError, ValueError, OSError, TimeoutError) as exc:
        print(json.dumps({"status": "blocked", "reason": getattr(exc, "reason", type(exc).__name__)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
