from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.onboarding import build_first_run_summary, default_data_dir, run_first_run_setup
from quantdesk.shared import truthy


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QuantDesk first-run wizard")
    parser.add_argument("--config-path", default="")
    parser.add_argument("--data-dir", default=str(default_data_dir()))
    parser.add_argument("--install-sample-data", default="true")
    parser.add_argument("--network", default="false")
    parser.add_argument("--profile", default="both", choices=["long_term", "intraday_observer", "both"])
    parser.add_argument("--schedule-daily-1600", default="false")
    parser.add_argument("--init-live-account", default="true")
    parser.add_argument("--force-live-init", default="false")
    parser.add_argument("--run-self-check", default="true")
    parser.add_argument("--non-interactive", nargs="?", const="true", default="true")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", default="false")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not truthy(args.non_interactive):
        print("Interactive mode uses the same safe defaults; press Enter to accept prompts.")
        if not args.data_dir:
            args.data_dir = input(f"Data directory [{default_data_dir()}]: ").strip() or str(default_data_dir())
    payload = run_first_run_setup(
        config_path=args.config_path or None,
        data_dir=args.data_dir,
        install_sample_data=truthy(args.install_sample_data),
        network_enabled=truthy(args.network),
        profile=args.profile,
        schedule_daily_1600=truthy(args.schedule_daily_1600),
        init_live_account=truthy(args.init_live_account),
        force_live_init=truthy(args.force_live_init),
        run_self_check=truthy(args.run_self_check),
    )
    output = build_first_run_summary(payload) if truthy(args.summary) else payload
    if truthy(args.json):
        print(json.dumps(output, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    else:
        print(f"first_run_wizard status={output.get('status')} config={output.get('config_path') or (output.get('config') or {}).get('config_path')}")
        for command in payload.get("next_commands") or []:
            print(f"next: {command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
