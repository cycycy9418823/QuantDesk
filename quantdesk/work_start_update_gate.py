from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.intraday_snapshot_updater import run_intraday_snapshot_update
from quantdesk.market_session_detector import detect_market_session, write_market_session
from quantdesk.post_close_daily_updater import run_post_close_daily_update
from quantdesk.research_outputs_refresh import refresh_research_outputs
from quantdesk.shared import read_json as shared_read_json
from quantdesk.strategy_lab.research_signal_safety import boundary_payload


def _read_json(path: Path) -> dict[str, Any]:
    loaded = shared_read_json(path, {})
    return loaded if isinstance(loaded, dict) else {}


def preflight(root: Path) -> dict[str, Any]:
    checks = [
        "results/data_repair_verification/v423_qoderwork_data_repair_verification_handoff_latest.json",
        "results/daily_research/v421_daily_open_workflow_handoff_latest.json",
        "results/long_loop/v422_daily_research_long_loop_handoff_latest.json",
        "results/project_state/current_project_state_latest.json",
        "results/candidate_status_reconciliation_latest.json",
        "docs/current_project_state.md",
        "data/daily",
        "data/features/daily_features_latest.csv",
        "data/universe/stock_theme_map.csv",
        "quantdesk/data_update_orchestrator.py",
        "scripts/run_data_update.py",
    # v421 workflow entrypoint retired (dead legacy shim, 2026-08-31);
    # show/result/long-loop v42x entries are also legacy paths that no longer
    # exist in the current product closure.
    ]
    file_checks = [{"path": rel, "exists": (root / rel).exists()} for rel in checks]
    candidate = _read_json(root / "results/candidate_status_reconciliation_latest.json")
    v423 = _read_json(root / "results/data_repair_verification/v423_qoderwork_data_repair_verification_handoff_latest.json")
    session = detect_market_session(root)
    payload = {
        "task_id": "v424_preflight",
        "status": "v424_preflight_passed" if all(row["exists"] for row in file_checks) else "v424_preflight_passed_with_warnings",
        "v423_completed": str(v423.get("final_conclusion", "")).startswith("v423_"),
        "data_daily_readable": (root / "data/daily").exists(),
        "latest_local_trade_date": session["latest_local_trade_date"],
        "expected_latest_trade_date": session["expected_latest_completed_trade_date"],
        "data_update_cli_signature_previously_blocked": v423.get("data_update_cli_usable") is False,
        "candidate_upgrade_allowed": candidate.get("candidate_upgrade_allowed"),
        "post_close_new_date_daily_write_allowed_with_validation": True,
        "trading_functions_prohibited": True,
        "file_checks": file_checks,
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v424_preflight_latest.json", payload)
    write_md(root / "reports/data_update/v424_preflight_latest.md", "# v424 Preflight\n\n" + md_table([payload], ["status", "v423_completed", "data_daily_readable", "latest_local_trade_date", "expected_latest_trade_date", "candidate_upgrade_allowed"]))
    return payload


def _update_failure_reason(update_result: dict[str, Any]) -> str:
    direct_reason = str(update_result.get("failure_reason") or "").strip()
    if direct_reason:
        return direct_reason
    failures = [str(item) for item in update_result.get("failures") or [] if str(item).strip()]
    if failures:
        return "; ".join(failures[:5])
    status = str(update_result.get("status") or "").strip()
    if status == "skipped_already_fresh":
        return ""
    if status.startswith(("provider_failed", "failed", "skipped", "network_false")):
        return status
    return ""


def run_work_start_update_gate(
    root: Path,
    *,
    network: bool = False,
    max_symbols: int = 300,
    scope: str = "daily",
    force_check: bool = False,
    dry_run: bool = False,
    strict: bool = False,
    provider: str = "akshare",
    timeout_seconds: int = 12,
) -> dict[str, Any]:
    pre = preflight(root)
    session = write_market_session(root, detect_market_session(root))
    update_result: dict[str, Any]
    if not network:
        update_result = {
            "task_id": "v424_update_decision",
            "status": "network_false_status_only",
            "attempted": False,
            "network": False,
            "session": session["session"],
            "latest_local_trade_date": session["latest_local_trade_date"],
            "expected_latest_completed_trade_date": session["expected_latest_completed_trade_date"],
            "freshness_status": session["freshness_status"],
            "failure_reason": "network=false; use --network true for bounded update",
            "fallback": "local_data_status_only",
            "rows_written": 0,
        }
        refresh = {"status": "skipped_no_update", "refresh_attempted": False}
    elif (
        session.get("data_fresh_for_latest_trading_day") is True
        and not force_check
        and scope != "intraday"
        and session.get("recommended_update_mode") in {"post_close_daily_update", "check_previous_trading_day"}
    ):
        update_result = {
            "task_id": "v424_update_decision",
            "status": "skipped_already_fresh",
            "attempted": False,
            "network": True,
            "session": session["session"],
            "latest_local_trade_date": session["latest_local_trade_date"],
            "expected_latest_completed_trade_date": session["expected_latest_completed_trade_date"],
            "freshness_status": session["freshness_status"],
            "failure_reason": "",
            "fallback": "local_data_already_fresh",
            "rows_written": 0,
            "rows_skipped": 0,
            "symbols_attempted": 0,
            "symbols_updated": 0,
        }
        refresh = {"status": "skipped_already_fresh", "refresh_attempted": False}
    elif session["recommended_update_mode"] == "intraday_snapshot_update" or scope == "intraday":
        update_result = run_intraday_snapshot_update(root, network=True, max_symbols=max_symbols, provider=provider, timeout_seconds=timeout_seconds)
        refresh = refresh_research_outputs(root, reason="intraday_snapshot_attempt", network=False, max_symbols=max_symbols)
    elif session["recommended_update_mode"] in {"post_close_daily_update", "check_previous_trading_day"}:
        update_result = run_post_close_daily_update(root, network=True, max_symbols=max_symbols, dry_run=dry_run, provider=provider, timeout_seconds=timeout_seconds)
        refresh = refresh_research_outputs(root, reason="post_close_daily_attempt", network=False, max_symbols=max_symbols) if update_result.get("rows_written", 0) > 0 else {"status": "skipped_no_rows_written", "refresh_attempted": False}
    else:
        update_result = {"task_id": "v424_update_decision", "status": "skipped_non_trading_day", "attempted": False, "network": True, "session": session["session"], "rows_written": 0, "failure_reason": "non_trading_day", "fallback": "local_data_status_only"}
        refresh = {"status": "skipped_non_trading_day", "refresh_attempted": False}
    payload = {
        "task_id": "v424_work_start_update_gate",
        "status": "completed_with_warnings" if update_result.get("status", "").startswith(("provider_failed", "network_false")) else "completed",
        "network": bool(network),
        "max_symbols": int(max_symbols),
        "scope": scope,
        "force_check": bool(force_check),
        "dry_run": bool(dry_run),
        "strict": bool(strict),
        "provider": provider,
        "timeout_seconds": int(timeout_seconds),
        "session": session,
        "update_result": update_result,
        "research_refresh": refresh,
        "user_summary": {
            "current_data_date": session["latest_local_trade_date"],
            "expected_latest_completed_trade_date": session["expected_latest_completed_trade_date"],
            "is_latest": session["data_fresh_for_latest_trading_day"],
            "update_attempted": update_result.get("attempted", False),
            "symbols_attempted": update_result.get("symbols_attempted", 0),
            "symbols_updated": update_result.get("symbols_updated", 0),
            "rows_written": update_result.get("rows_written", 0),
            "rows_skipped": update_result.get("rows_skipped", 0),
            "provider_failure_count": len(update_result.get("failures") or []),
            "failure_reason": _update_failure_reason(update_result),
            "fallback": update_result.get("fallback", update_result.get("status", "")),
        },
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v424_work_start_update_gate_latest.json", payload)
    write_md(root / "reports/data_update/v424_work_start_update_gate_latest.md", "# v424 Work-Start Update Gate\n\n" + md_table([payload["user_summary"]], ["current_data_date", "expected_latest_completed_trade_date", "is_latest", "update_attempted", "symbols_attempted", "symbols_updated", "rows_written", "rows_skipped", "provider_failure_count", "failure_reason", "fallback"]))
    return payload


def write_review_pack(root: Path) -> dict[str, Any]:
    files = [
        "results/data_update/v424_preflight_latest.json",
        "reports/data_update/v424_preflight_latest.md",
        "results/data_update/v424_market_session_latest.json",
        "reports/data_update/v424_market_session_latest.md",
        "results/data_update/v424_data_update_cli_fix_latest.json",
        "reports/data_update/v424_data_update_cli_fix_latest.md",
        "results/data_update/v424_work_start_update_gate_latest.json",
        "reports/data_update/v424_work_start_update_gate_latest.md",
        "results/data_update/v424_app_open_refresh_latest.json",
        "reports/data_update/v424_app_open_refresh_latest.md",
        "results/daily_research/v424_open_app_result_page_latest.json",
        "reports/daily_research/v424_open_app_result_page_latest.md",
        "results/data_update/v424_intraday_snapshot_update_latest.json",
        "reports/data_update/v424_intraday_snapshot_update_latest.md",
        "results/data_update/v424_post_close_daily_update_latest.json",
        "reports/data_update/v424_post_close_daily_update_latest.md",
        "results/data_update/v424_research_outputs_refresh_latest.json",
        "reports/data_update/v424_research_outputs_refresh_latest.md",
        "results/data_update/v424_data_update_status_latest.json",
        "reports/data_update/v424_data_update_status_latest.md",
        "results/data_update/v424_v421_v422_integration_latest.json",
        "reports/data_update/v424_v421_v422_integration_latest.md",
        "results/data_update/v424_validation_latest.json",
        "reports/data_update/v424_validation_latest.md",
        "results/data_update/v424_work_start_auto_update_gate_handoff_latest.json",
        "reports/data_update/v424_work_start_auto_update_gate_handoff_latest.md",
        "agent_workspace/next_codex_task_after_v424_work_start_auto_update_gate.md",
        "docs/v424_work_start_auto_update_gate.md",
        "docs/v424_open_app_refresh_workflow.md",
        "docs/v424_data_update_policy.md",
    ]
    readme = root / "dist/quantdesk_v4.2.4_work_start_auto_update_gate_review_README.md"
    manifest_path = root / "dist/quantdesk_v4.2.4_work_start_auto_update_gate_review_manifest.json"
    zip_path = root / "dist/quantdesk_v4.2.4_work_start_auto_update_gate_review_pack.zip"
    write_md(readme, "# QuantDesk v4.2.4 Work-Start Auto Update Gate Review Pack\n\nResearch-only. Not a release. No broker, live trading, auto execution, buy/sell advice, or real-money allocation.\n")
    files += [
        "dist/quantdesk_v4.2.4_work_start_auto_update_gate_review_manifest.json",
        "dist/quantdesk_v4.2.4_work_start_auto_update_gate_review_README.md",
    ]
    manifest = {"pack_name": "quantdesk_v4.2.4_work_start_auto_update_gate_review_pack", "status": "completed_with_warnings", "missing_files": [], "included_files": [], "file_count": 0, "zip_size_bytes": 0, "candidate_upgrade_allowed": False, "not_a_release": True, **boundary_payload()}
    write_json(manifest_path, manifest)
    manifest["included_files"] = [rel for rel in files if (root / rel).exists()]
    manifest["missing_files"] = [rel for rel in files if not (root / rel).exists()]
    manifest["file_count"] = len(manifest["included_files"])
    write_json(manifest_path, manifest)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(set(manifest["included_files"])):
            zf.write(root / rel, arcname=rel)
    manifest["zip_size_bytes"] = zip_path.stat().st_size
    write_json(manifest_path, manifest)
    return manifest
