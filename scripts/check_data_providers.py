from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_provider_registry import data_provider_status_payload, save_data_provider_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check QuantDesk data-provider diagnostics.")
    parser.add_argument(
        "--check-network",
        action="store_true",
        help="Allow provider network smoke checks where implemented. The default is dependency/local checks only.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    parser.add_argument("--write", action="store_true", help="Write results/data_provider_status_latest.json.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = save_data_provider_status(check_network=args.check_network) if args.write else data_provider_status_payload(check_network=args.check_network)
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0
    print("QuantDesk data provider diagnostics")
    print(f"check_network={payload['check_network']}")
    print(f"status_json={payload['path']}")
    summary = payload.get("summary", {})
    print("summary=" + json.dumps(summary.get("counts", {}), ensure_ascii=False, sort_keys=True))
    for provider in payload.get("providers", []):
        deps = provider.get("dependency_status") or []
        missing = [str(dep.get("name")) for dep in deps if isinstance(dep, dict) and not dep.get("installed")]
        suffix = f"; missing={','.join(missing)}" if missing else ""
        error = provider.get("last_error") or ""
        print(
            f"- {provider.get('type')}::{provider.get('name')} "
            f"status={provider.get('status')} network_required={provider.get('network_required')}{suffix} "
            f"last_error={error}"
        )
    print("Note: default mode does not connect to external data sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
