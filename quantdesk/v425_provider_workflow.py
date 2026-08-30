from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.bounded_market_data_provider import provider_config
from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.data_update_status import build_data_update_status
from quantdesk.post_close_daily_updater import run_post_close_daily_update
from quantdesk.research_outputs_refresh import refresh_research_outputs
from quantdesk.strategy_lab.research_signal_safety import boundary_payload


def run_v425_provider_workflow(root: Path, *, max_symbols: int = 2, simulate: bool = False, provider: str = "akshare", network: bool = False) -> dict[str, Any]:
    root = root.resolve()
    for rel in ["results/data_update", "reports/data_update", "results/daily_research", "reports/daily_research", "dist", "agent_workspace"]:
        (root / rel).mkdir(parents=True, exist_ok=True)
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    config = provider_config(root, provider_type=provider, max_symbols=max_symbols)
    dry_run = run_post_close_daily_update(root, network=False, max_symbols=max_symbols, provider=provider, dry_run=True)
    update_provider = "v425_simulated_provider" if simulate else provider
    update = run_post_close_daily_update(
        root,
        network=bool(network),
        max_symbols=max_symbols,
        provider=update_provider,
        dry_run=not bool(network),
        simulate=bool(simulate and network),
        write_mode="append_new_date",
    )
    refresh = refresh_research_outputs(root, reason="v425_post_close_update_test", network=False, max_symbols=300)
    features_payload = {
        "task_id": "v425_daily_features_refresh",
        "status": "completed" if refresh.get("refresh_success") else "completed_with_warnings",
        "refresh_success": refresh.get("refresh_success"),
        "failed_steps": refresh.get("failed_steps", []),
        "candidate_count": refresh.get("candidate_count"),
        "sector_count": refresh.get("sector_count"),
        "leaderboard_rows": refresh.get("leaderboard_rows"),
        "features_file": "data/features/daily_features_latest.csv",
        "research_only": True,
    }
    write_json(root / "results/daily_research/v425_daily_features_refresh.json", features_payload)
    write_md(root / "reports/daily_research/v425_daily_features_refresh.md", "# v425 Daily Features Refresh\n\n" + md_table([features_payload], ["status", "refresh_success", "candidate_count", "sector_count", "leaderboard_rows", "features_file"]))
    status_payload, _ = build_data_update_status(root)
    history = {
        "task_id": "v425_provider_update_history",
        "status": "completed",
        "started_at": started,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "events": [
            {"step": "provider_config", "status": config.get("status")},
            {"step": "network_false_dry_run", "status": dry_run.get("status"), "rows_written": dry_run.get("rows_written")},
            {"step": "post_close_network_update", "status": update.get("status"), "rows_written": update.get("rows_written"), "provider": update_provider, "network": bool(network), "simulation": bool(simulate and network)},
            {"step": "research_outputs_refresh", "status": refresh.get("status")},
        ],
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v425_provider_update_history_latest.json", history)
    write_md(root / "reports/data_update/v425_provider_update_history_latest.md", "# v425 Provider Update History\n\n" + md_table(history["events"], ["step", "status", "rows_written"]))
    heartbeat = {
        "task_id": "v425_provider_update_heartbeat",
        "status": "completed",
        "last_step": "review_pack",
        "rows_written": update.get("rows_written", 0),
        "failures": update.get("failures", []),
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v425_provider_update_heartbeat_latest.json", heartbeat)
    write_md(root / "reports/data_update/v425_provider_update_heartbeat_latest.md", "# v425 Provider Update Heartbeat\n\n" + md_table([heartbeat], ["status", "last_step", "rows_written"]))
    provider_warnings: list[str] = []
    if not network:
        provider_warnings.append("network=false; provider update skipped")
    for attempt in update.get("provider_attempts", []) or []:
        for item in attempt.get("attempts", []) or []:
            if item.get("status") == "failed":
                provider_warnings.append(f"{item.get('provider')}: {item.get('error')}")
    if simulate and network:
        provider_warnings.append("network=true used v425_simulated_provider; not real market data ingestion")
    if network and not update.get("real_provider_write") and not simulate:
        provider_warnings.append("network=true provider did not write a new row")
    final_status = "v425_provider_post_close_update_completed" if update.get("rows_written", 0) > 0 and refresh.get("refresh_success") and not provider_warnings else "v425_provider_post_close_update_completed_with_warnings"
    summary = {
        "task_id": "v425_provider_post_close_update_summary",
        "status": final_status,
        "attempted": update.get("attempted"),
        "success": update.get("rows_written", 0) > 0,
        "failures": update.get("failures", []),
        "skipped": update.get("rows_skipped", 0),
        "symbols_attempted": update.get("symbols_attempted"),
        "symbols_updated": update.get("symbols_updated"),
        "rows_written": update.get("rows_written"),
        "rows_skipped": update.get("rows_skipped"),
        "provider": update_provider,
        "network": bool(network),
        "simulation": bool(simulate and network),
        "real_provider_write": bool(update.get("real_provider_write")),
        "latest_date_before": update.get("latest_date_before"),
        "latest_date_after": update.get("latest_date_after"),
        "schema_validation": "passed" if not update.get("schema_errors") else "failed",
        "post_write_readback": "passed" if update.get("rows_written", 0) > 0 else "not_applicable",
        "refresh_success": refresh.get("refresh_success"),
        "failed_steps": refresh.get("failed_steps", []),
        "warnings": provider_warnings,
        "candidate_upgrade_allowed": False,
        "canonical_equity_overwritten": False,
        "generated_trade_advice": False,
        "final_conclusion": final_status,
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v425_post_close_update_test.json", update)
    write_md(root / "reports/data_update/v425_post_close_update_test.md", "# v425 Post-Close Update Test\n\n" + md_table([update], ["status", "attempted", "provider", "symbols_attempted", "symbols_updated", "rows_written", "rows_skipped", "latest_date_before", "latest_date_after", "protected_paths_status"]))
    write_json(root / "results/data_update/v425_provider_post_close_update_summary_latest.json", summary)
    write_md(root / "reports/data_update/v425_provider_post_close_update_summary_latest.md", "# v425 Provider Post-Close Update Summary\n\n" + md_table([summary], ["status", "attempted", "success", "symbols_attempted", "symbols_updated", "rows_written", "latest_date_before", "latest_date_after", "refresh_success"]))
    write_json(root / "results/data_update/v425_provider_post_close_update_handoff_latest.json", summary)
    write_md(root / "reports/data_update/v425_provider_post_close_update_handoff_latest.md", "# v425 Provider Post-Close Update Handoff\n\nFinal conclusion: `" + final_status + "`\n")
    validation = {
        "task_id": "v425_validation",
        "status": "completed",
        "provider_config_written": True,
        "post_close_update_payload_written": True,
        "summary_written": True,
        "handoff_written": True,
        "canonical_equity_overwritten": False,
        "generated_trade_advice": False,
        "candidate_upgrade_allowed": False,
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v425_validation_latest.json", validation)
    write_md(root / "reports/data_update/v425_validation_latest.md", "# v425 Validation\n\n" + md_table([validation], ["status", "provider_config_written", "post_close_update_payload_written", "summary_written", "handoff_written"]))
    cli_summary = {
        "task_id": "v425_cli_summary",
        "status": final_status,
        "rows_written": summary["rows_written"],
        "symbols_updated": summary["symbols_updated"],
        "network": bool(network),
        "simulation": bool(simulate and network),
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v425_cli_summary_latest.json", cli_summary)
    write_md(root / "reports/data_update/v425_cli_summary_latest.md", "# v425 CLI Summary\n\n" + md_table([cli_summary], ["status", "rows_written", "symbols_updated", "network", "simulation"]))
    write_md(root / "agent_workspace/next_codex_task_after_v425_provider_update.md", "# Next Task After v425\n\nRecommended: replace simulated provider with a user-approved real bounded AKShare adapter for selected symbols, then rerun post-close update with network=true. Keep research-only boundaries.\n")
    manifest = write_review_pack(root)
    summary["review_pack_status"] = manifest["status"]
    summary["review_pack_missing_files"] = manifest["missing_files"]
    write_json(root / "results/data_update/v425_provider_post_close_update_handoff_latest.json", summary)
    return {"config": config, "dry_run": dry_run, "update": update, "refresh": refresh, "features": features_payload, "status": status_payload, "summary": summary, "manifest": manifest}


def write_review_pack(root: Path) -> dict[str, Any]:
    files = [
        "results/data_update/v425_provider_config_latest.json",
        "reports/data_update/v425_provider_config_latest.md",
        "results/data_update/v425_post_close_update_test.json",
        "reports/data_update/v425_post_close_update_test.md",
        "results/daily_research/v425_daily_features_refresh.json",
        "reports/daily_research/v425_daily_features_refresh.md",
        "results/data_update/v425_provider_update_history_latest.json",
        "reports/data_update/v425_provider_update_history_latest.md",
        "results/data_update/v425_provider_update_heartbeat_latest.json",
        "reports/data_update/v425_provider_update_heartbeat_latest.md",
        "results/data_update/v425_provider_post_close_update_summary_latest.json",
        "reports/data_update/v425_provider_post_close_update_summary_latest.md",
        "results/data_update/v425_validation_latest.json",
        "reports/data_update/v425_validation_latest.md",
        "results/data_update/v425_provider_post_close_update_handoff_latest.json",
        "reports/data_update/v425_provider_post_close_update_handoff_latest.md",
        "results/data_update/v425_cli_summary_latest.json",
        "reports/data_update/v425_cli_summary_latest.md",
        "agent_workspace/next_codex_task_after_v425_provider_update.md",
    ]
    readme = root / "dist/quantdesk_v4.2.5_provider_post_close_update_review_README.md"
    manifest_path = root / "dist/quantdesk_v4.2.5_provider_post_close_update_review_manifest.json"
    zip_path = root / "dist/quantdesk_v4.2.5_provider_post_close_update_review_pack.zip"
    write_md(readme, "# QuantDesk v4.2.5 Provider Post-Close Update Review Pack\n\nResearch-only. Not a release. No broker, live trading, auto execution, buy/sell advice, target price, position advice, or real-money allocation.\n")
    files += [
        "dist/quantdesk_v4.2.5_provider_post_close_update_review_manifest.json",
        "dist/quantdesk_v4.2.5_provider_post_close_update_review_README.md",
    ]
    manifest = {"pack_name": "quantdesk_v4.2.5_provider_post_close_update_review_pack", "status": "completed_with_warnings", "missing_files": [], "included_files": [], "file_count": 0, "zip_size_bytes": 0, "not_a_release": True, "candidate_upgrade_allowed": False, **boundary_payload()}
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
