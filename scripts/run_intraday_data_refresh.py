"""CLI entry for the intraday minute refresh (dynamic scope, fail-closed).

One invocation = one collection round. The scheduled task fires this every
5 minutes inside the market windows (see install_intraday_data_refresh_task.ps1);
each round re-resolves the dynamic scope, fetches only missing minutes for
those symbols, and writes the health manifest + optional alert dispatch.

Research only. No broker, no real orders, not investment advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday_refresh.alerts import dispatch_intraday_alerts  # noqa: E402
from quantdesk.intraday_refresh.collector import (  # noqa: E402
    DEFAULT_MAX_ROUND_SECONDS,
    collect_intraday_refresh,
    reconcile_abandoned_round,
)


def observe_all_profiles(root: Path) -> tuple[list[dict], dict, dict]:
    """Feed every configured AI-trader book from this round's data.

    Returns ``(profile_runs, default_notes, default_intents)``. Each book only
    reads the shared market data; the note/intent files stay per-book. This is
    a separate function so callers (and tests) can neutralise it without
    touching real book state.
    """

    profile_runs: list[dict] = []
    default_notes: dict = {}
    default_intents: dict = {}
    try:
        from ai_trader.paths import configured_profiles, profile_paths
    except Exception as exc:  # noqa: BLE001 - observation must not fail the data round
        return ([{"profile": "default", "error": f"paths_import_failed:{type(exc).__name__}"}], {}, {})
    for profile in configured_profiles():
        label = profile or "default"
        runtime = profile_paths(profile)
        note_result: dict = {}
        try:
            from ai_trader.daytime import observe_session

            note_result = observe_session(root, paths=runtime)
        except Exception as exc:  # noqa: BLE001 - notes must not fail the data round
            note_result = {
                "ok": False,
                "new_notes": 0,
                "reason": f"daytime_observe_failed:{type(exc).__name__}",
            }
        intent_run: dict = {}
        try:
            from ai_trader.daytime_intent import observe_intents

            intent_run = observe_intents(root, paths=runtime)
        except Exception as exc:  # noqa: BLE001 - intents must not fail the data round
            intent_run = {
                "ok": False,
                "new_intents": 0,
                "reason": f"daytime_intent_failed:{type(exc).__name__}",
            }
        profile_runs.append({
            "profile": label,
            "ok": note_result.get("ok"),
            "new_notes": note_result.get("new_notes", 0),
            "intents_ok": intent_run.get("ok"),
            "new_intents": intent_run.get("new_intents", 0),
        })
        if not profile:
            default_notes, default_intents = note_result, intent_run
    return profile_runs, default_notes, default_intents


def main(argv: list[str] | None = None) -> int:
    from quantdesk.shared import truthy

    parser = argparse.ArgumentParser(description="Intraday minute data refresh (dynamic scope).")
    parser.add_argument("--enable-network", action="store_true", help="Explicit network authorization.")
    parser.add_argument("--network", default="false")
    parser.add_argument("--enable-alert-network", action="store_true")
    parser.add_argument("--alert-push", action="store_true")
    parser.add_argument("--alert-dry-run", default="true")
    parser.add_argument("--no-alerts", action="store_true")
    parser.add_argument("--json", default="true")
    parser.add_argument("--read-ai-minute-focus", action="store_true",
                        help="Read same-day model focus only for books with existing level-3 authorization.")
    parser.add_argument(
        "--max-round-seconds",
        type=float,
        default=DEFAULT_MAX_ROUND_SECONDS,
        help=(
            "Bound the round's own wall clock so it stops before the task's "
            "execution limit and still writes a manifest."
        ),
    )
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help=(
            "Publish a terminal 'abandoned' state for a previous running round "
            "whose process is gone, then exit. Fetches nothing."
        ),
    )
    args = parser.parse_args(argv)

    if args.reconcile_only:
        result = reconcile_abandoned_round(PROJECT_ROOT)
        if truthy(args.json):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    network = args.enable_network or truthy(args.network)
    alert_network = bool(args.enable_alert_network)
    push = bool(args.alert_push)
    dry_run = truthy(args.alert_dry_run)

    focus_options = {"read_ai_minute_focus": True} if args.read_ai_minute_focus else {}
    manifest = collect_intraday_refresh(
        PROJECT_ROOT, network=network, max_round_seconds=float(args.max_round_seconds),
        **focus_options,
    )

    profile_runs, observe_result, intent_result = observe_all_profiles(PROJECT_ROOT)

    alert_result: dict = {}
    if not args.no_alerts:
        alert_result = dispatch_intraday_alerts(
            PROJECT_ROOT,
            network=alert_network,
            push=push,
            dry_run=dry_run,
            intraday_manifest=manifest,
        )

    if truthy(args.json):
        print(
            json.dumps(
                {
                    "intraday": manifest,
                    "alerts": {
                        "status": alert_result.get("status"),
                        "reason_code": alert_result.get("reason_code"),
                        "event_count": alert_result.get("event_count"),
                    },
                    "daytime": {
                        "ok": observe_result.get("ok"),
                        "new_notes": observe_result.get("new_notes"),
                        "events": observe_result.get("events") or [],
                    },
                    "daytime_intents": {
                        "ok": intent_result.get("ok"),
                        "new_intents": intent_result.get("new_intents"),
                        "events": intent_result.get("events") or [],
                    },
                    "daytime_profiles": profile_runs,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    status = manifest.get("status")
    if status == "timed_out":
        return 5
    if status == "blocked":
        return 3
    if status == "already_running":
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
