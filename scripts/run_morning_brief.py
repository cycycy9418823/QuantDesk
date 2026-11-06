from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.morning_brief import DISCLAIMER, run_morning_brief  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


SENDKEY_ENV = "QUANTDESK_SERVERCHAN_SENDKEY"
# The key predates this task under an older name. Read the legacy variable as a
# fallback so an existing machine keeps working, preferring the canonical name.
# Neither value is ever printed, logged, or written to a receipt.
LEGACY_SENDKEY_ENV = "SERVERCHAN_SEND_KEY"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a verified local QuantDesk research morning brief. External "
            "ServerChan delivery requires explicit network and push flags."
        )
    )
    # No --project-root: the brief reads real chain artifacts and writes real
    # receipts, so the root is pinned to this checkout rather than accepted from
    # the caller.
    parser.add_argument("--network", default="false")
    parser.add_argument("--enable-network", action="store_true")
    # Explicit value form so callers can say "--push false" without argparse
    # treating "false" as a positional; the bare flag still means true for the
    # scheduled-task form.
    parser.add_argument("--push", nargs="?", const="true", default="")
    parser.add_argument("--dry-run", default="")
    parser.add_argument("--allow-outside-window", action="store_true")
    parser.add_argument("--json", default="true")
    parser.add_argument("--summary", default="false")
    return parser


def _load_sendkey() -> str | None:
    for name in (SENDKEY_ENV, LEGACY_SENDKEY_ENV):
        value = os.environ.get(name)
        if value and value.strip():
            return value
    return None


def _exit_code(payload: dict) -> int:
    status = str(payload.get("status") or "")
    reason = str(payload.get("reason_code") or "")
    if status in {"preview_generated", "sent", "skipped_duplicate", "skipped_non_trading_day"}:
        return 0
    if status == "busy":
        return 3
    if reason in {"snapshot_blocked", "verified_snapshot_unavailable"}:
        return 2
    if status in {"failed", "ambiguous"}:
        return 5
    return 4


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    network = bool(args.enable_network or truthy(args.network))
    push = truthy(args.push) if str(args.push).strip() else False
    dry_run = truthy(args.dry_run) if str(args.dry_run).strip() else not (network and push)
    payload = run_morning_brief(
        PROJECT_ROOT,
        network=network,
        push=push,
        dry_run=dry_run,
        allow_outside_window=bool(args.allow_outside_window),
        _sendkey_loader=_load_sendkey,
    )
    if truthy(args.json):
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=None if truthy(args.summary) else 2,
                allow_nan=False,
            )
        )
    else:
        print(
            "morning-brief "
            f"status={payload.get('status')} "
            f"reason={payload.get('reason_code')} "
            f"as_of={payload.get('as_of', '')} "
            f"external_sent_count={payload.get('external_sent_count', 0)}"
        )
        print(DISCLAIMER)
    return _exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())
