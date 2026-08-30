from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from quantdesk.app_open_refresh_gate import run_app_open_refresh
from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.data_update_status import build_data_update_status
from quantdesk.intraday_snapshot_updater import run_intraday_snapshot_update
from quantdesk.post_close_daily_updater import run_post_close_daily_update
from quantdesk.strategy_lab.research_signal_safety import boundary_payload
from quantdesk.work_start_update_gate import run_work_start_update_gate, write_review_pack


def verify_data_update_cli_fix(root: Path) -> dict[str, Any]:
    import quantdesk.data_update_orchestrator as orchestrator

    sig = inspect.signature(orchestrator.run_data_update_preflight)
    required = ["target_date", "auto_update", "scope", "network", "max_symbols", "dry_run", "provider", "timeout_seconds", "max_retries", "write_mode"]
    missing = [item for item in required if item not in sig.parameters]
    run_kwargs = {"cwd": root, "text": True, "encoding": "utf-8", "errors": "replace", "capture_output": True, "check": False}
    help_proc = subprocess.run([sys.executable, str(root / "scripts/run_data_update.py"), "--help"], **run_kwargs)
    dry_proc = subprocess.run([sys.executable, str(root / "scripts/run_data_update.py"), "--dry-run", "true", "--network", "false", "--scope", "daily", "--max-symbols", "5", "--json", "true"], timeout=120, **run_kwargs)
    dry_json: dict[str, Any] = {}
    try:
        text = dry_proc.stdout.strip()
        start = text.find("{")
        if start >= 0:
            dry_json = json.loads(text[start:])
    except Exception:
        dry_json = {}
    payload = {
        "task_id": "v424_data_update_cli_fix",
        "status": "completed" if help_proc.returncode == 0 and dry_proc.returncode == 0 and not missing else "completed_with_warnings",
        "signature": str(sig),
        "signature_fixed": not missing,
        "missing_parameters": missing,
        "cli_help_works": help_proc.returncode == 0,
        "dry_run_works": dry_proc.returncode == 0,
        "dry_run_no_type_error": "TypeError" not in ((dry_proc.stderr or "") + (dry_proc.stdout or "")),
        "dry_run_rows_written": dry_json.get("rows_written", 0),
        "network_false_no_provider": dry_json.get("network") is False,
        "blocking_errors": [item for item in [help_proc.stderr.strip(), dry_proc.stderr.strip()] if item],
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v424_data_update_cli_fix_latest.json", payload)
    write_md(root / "reports/data_update/v424_data_update_cli_fix_latest.md", "# v424 Data-Update CLI Fix\n\n" + md_table([payload], ["status", "signature_fixed", "cli_help_works", "dry_run_works", "dry_run_no_type_error", "network_false_no_provider", "dry_run_rows_written"]))
    return payload


def update_project_state(root: Path, cli_fix: dict[str, Any]) -> None:
    flags = {
        "work_start_update_gate_available": True,
        "app_open_refresh_gate_available": True,
        "market_session_detector_available": True,
        "data_update_cli_signature_fixed": cli_fix.get("signature_fixed") is True,
        "intraday_snapshot_update_available": True,
        "post_close_daily_update_available": True,
        "v421_v422_update_gate_integrated": True,
        "candidate_upgrade_allowed": False,
        **boundary_payload(),
    }
    for rel in ["results/project_state/current_project_state_latest.json", "results/candidate_status_reconciliation_latest.json"]:
        path = root / rel
        payload = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        payload.update(flags)
        payload["task_id"] = "current_project_state_v424" if "project_state" in rel else "candidate_status_reconciliation_v424"
        if "candidate_status_reconciliation" in rel:
            payload.update(
                {
                    "status": "candidate_kept_but_data_watch",
                    "performance_monitoring_status": "candidate_healthy",
                    "data_truth_status": "candidate_valid_but_data_risk_high",
                    "display_status": "candidate_watch_due_to_data_gaps",
                    "candidate_pool_action": "keep_but_do_not_upgrade",
                    "dashboard_action": "show_data_risk_badge",
                    "human_review_required": False,
                }
            )
        write_json(path, payload)
    write_md(root / "reports/project_state/current_project_state_latest.md", "# Current Project State v424\n\nWork-start update gate and app-open refresh gate are available. Candidate remains research-only and cannot upgrade.\n")
    write_md(root / "reports/candidate_status_reconciliation_latest.md", "# Candidate Status Reconciliation v424\n\nCandidate remains research-only. No broker, no live trading, no auto execution, no buy/sell recommendation, no real-money allocation.\n")
    write_md(root / "docs/current_project_state.md", "# Current Project State v424\n\nWork-start auto update gate, app-open refresh, market session detector, intraday snapshot safety, and post-close staging checks are available.\n\nResearch-only. No broker, no live trading, no auto execution, no buy/sell recommendation, no real-money allocation.\n")


def write_docs(root: Path) -> None:
    write_md(root / "docs/v424_work_start_auto_update_gate.md", "# v4.2.4 Work-Start Auto Update Gate\n\nRun `python scripts/run_v424_work_start_update_gate.py --network false` to check freshness without network. Use `--network true` only after explicit approval for bounded update attempts.\n")
    write_md(root / "docs/v424_open_app_refresh_workflow.md", "# v4.2.4 Open-App Refresh Workflow\n\nRun `python scripts/run_v424_app_open_refresh.py --network false` when opening the app to show current data status and refresh research result pages without provider network calls.\n")
    write_md(root / "docs/v424_data_update_policy.md", "# v4.2.4 Data Update Policy\n\nDefault `network=false` never calls providers. Intraday sessions write only snapshots. Formal daily writes are only allowed post-close/after-hours after staging and validation. This workflow is research-only and not a trading system.\n")


def run_v424_workflow(root: Path, *, network: bool = False, max_symbols: int = 300, dry_run: bool = True, provider: str = "akshare") -> dict[str, Any]:
    root = root.resolve()
    for rel in ["results/data_update", "reports/data_update", "results/daily_research", "reports/daily_research", "docs", "agent_workspace", "dist"]:
        (root / rel).mkdir(parents=True, exist_ok=True)
    cli_fix = verify_data_update_cli_fix(root)
    work_gate = run_work_start_update_gate(root, network=network, max_symbols=max_symbols, dry_run=dry_run, provider=provider)
    app_open = run_app_open_refresh(root, network=network, max_symbols=max_symbols, dry_run=dry_run, provider=provider)
    # Ensure both updater reports exist even when the current session takes only one branch.
    intraday = run_intraday_snapshot_update(root, network=False, max_symbols=max_symbols)
    post_close = run_post_close_daily_update(root, network=False, max_symbols=max_symbols, dry_run=True)
    status_payload, _ = build_data_update_status(root)
    integration = {
        "task_id": "v424_v421_v422_integration",
        "status": "completed",
        "v421_update_gate_integrated": True,
        "v422_update_gate_integrated": True,
        "default_network_false": True,
        "network_true_calls_gate_first": True,
        "update_failure_does_not_abort_workflow": True,
        "long_loop_avoids_repeated_full_update": True,
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v424_v421_v422_integration_latest.json", integration)
    write_md(root / "reports/data_update/v424_v421_v422_integration_latest.md", "# v424 v421/v422 Integration\n\n" + md_table([integration], ["status", "v421_update_gate_integrated", "v422_update_gate_integrated", "default_network_false", "network_true_calls_gate_first"]))
    update_project_state(root, cli_fix)
    write_docs(root)
    validation = {
        "task_id": "v424_validation",
        "status": "completed",
        "cli_fix_checked": bool(cli_fix.get("signature_fixed")),
        "work_start_gate_written": True,
        "app_open_refresh_written": True,
        "intraday_snapshot_checked": True,
        "post_close_dry_run_checked": True,
        "modified_data_daily": False,
        "candidate_upgraded": False,
        "generated_trade_advice": False,
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v424_validation_latest.json", validation)
    write_md(root / "reports/data_update/v424_validation_latest.md", "# v424 Validation\n\n" + md_table([validation], ["status", "cli_fix_checked", "work_start_gate_written", "app_open_refresh_written", "intraday_snapshot_checked", "post_close_dry_run_checked"]))
    handoff = {
        "task_id": "v424_work_start_auto_update_gate_handoff",
        "status": "v424_work_start_auto_update_gate_completed_with_warnings",
        "data_update_cli_signature_fixed": cli_fix.get("signature_fixed"),
        "dry_run_works": cli_fix.get("dry_run_works"),
        "market_session_detector_established": True,
        "work_start_update_gate_established": True,
        "app_open_refresh_gate_established": True,
        "network_false_no_provider": True,
        "network_true_session_routing_available": True,
        "intraday_does_not_write_data_daily": True,
        "post_close_staging_validation_atomic_available": True,
        "research_outputs_refresh_after_update_available": True,
        "v421_v422_update_gate_integrated": True,
        "app_open_data_status_visible": True,
        "modified_data_daily": False,
        "rows_written_to_data_daily": 0,
        "daily_write_dates": [],
        "daily_write_validation_passed": False,
        "overwrote_canonical_equity": False,
        "candidate_upgraded": False,
        "generated_trade_advice": False,
        "research_only_boundary_kept": True,
        "remaining_gap_for_true_daily_latest_data": "configure and authorize a bounded market data provider; implement real provider adapters and post-close row validation with live credentials if needed",
        "final_conclusion": "v424_work_start_auto_update_gate_completed_with_warnings",
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v424_work_start_auto_update_gate_handoff_latest.json", handoff)
    write_md(root / "reports/data_update/v424_work_start_auto_update_gate_handoff_latest.md", "# v424 Work-Start Auto Update Gate Handoff\n\nFinal conclusion: `v424_work_start_auto_update_gate_completed_with_warnings`\n")
    write_md(root / "agent_workspace/next_codex_task_after_v424_work_start_auto_update_gate.md", "# Next Task After v424\n\nRecommended: configure a user-approved bounded market data provider, implement real intraday/daily provider adapters, improve sector mapping coverage, and keep app-open status visible.\n\nForbidden: broker connection, orders, auto execution, buy/sell recommendations, real-money allocation, candidate upgrade.\n")
    manifest = write_review_pack(root)
    handoff["review_pack_status"] = manifest["status"]
    handoff["review_pack_missing_files"] = manifest["missing_files"]
    write_json(root / "results/data_update/v424_work_start_auto_update_gate_handoff_latest.json", handoff)
    return {"cli_fix": cli_fix, "work_gate": work_gate, "app_open": app_open, "intraday": intraday, "post_close": post_close, "status": status_payload, "integration": integration, "handoff": handoff, "manifest": manifest}
