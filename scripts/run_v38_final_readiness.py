from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.maintenance.v38_final_readiness import build_v38_final_readiness, write_v38_final_readiness  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build v3.8 final readiness audit without release or deletion.")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "results" / "v38_final_readiness_latest.json"))
    parser.add_argument("--report", default=str(PROJECT_ROOT / "reports" / "v38_final_readiness_latest.md"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = build_v38_final_readiness()
    files = write_v38_final_readiness(payload, output_json=args.output, report_md=args.report)
    payload["files_written"] = list(files.values())
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"v3.8 final readiness {payload['status']}: {payload['final_conclusion']}")
    return 0 if payload.get("status") in {"passed", "completed_with_warnings"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
