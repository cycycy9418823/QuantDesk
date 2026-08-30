from __future__ import annotations

import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.failed_symbol_replay import build_replay_queue
from quantdesk.full_market_catchup_updater import post_update_integrity, run_full_market_catchup
from quantdesk.provider_failure_classifier import classify_failure
from quantdesk.research_outputs_refresh import refresh_research_outputs
from quantdesk.shared import write_csv_rows
from quantdesk.stale_symbol_bucket import build_stale_buckets
from quantdesk.strategy_lab.research_signal_safety import boundary_payload
from quantdesk.symbol_market_normalizer import normalize_many
from quantdesk.update_progress_reporter import build_coverage_status
from quantdesk.update_queue_builder import latest_date_distribution


def _write_csv(path: Path, rows: list[dict[str, Any]], cols: list[str]) -> None:
    write_csv_rows(path, rows, fieldnames=cols)


def write_symbol_normalization(root: Path, symbols: list[str]) -> dict[str, Any]:
    rows = normalize_many(symbols)
    payload = {"task_id": "v427_symbol_normalization", "status": "completed", "symbols": rows, "symbol_count": len(rows), **boundary_payload()}
    write_json(root / "results/data_update/v427_symbol_normalization_latest.json", payload)
    write_md(root / "reports/data_update/v427_symbol_normalization_latest.md", "# v427 Symbol Normalization\n\n" + md_table(rows[:100], ["original_symbol", "normalized_symbol", "market", "provider_symbol", "support_status"]))
    _write_csv(root / "results/data_update/v427_symbol_normalization_latest.csv", rows, ["original_symbol", "normalized_symbol", "market", "provider_symbol", "support_status"])
    return payload


def classify_execution_failures(root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for row in records:
        if row.get("status") == "updated":
            continue
        rows.append(classify_failure(row.get("symbol", ""), row.get("failure_reason", ""), akshare_status="failed" if "akshare" in str(row.get("failure_reason", "")).lower() else "", tencent_status="failed"))
    payload = {"task_id": "v427_provider_failure_classification", "status": "completed", "classifications": rows, "classification_count": len(rows), "reason_counts": dict(Counter(row["final_failure_reason"] for row in rows)), **boundary_payload()}
    write_json(root / "results/data_update/v427_provider_failure_classification_latest.json", payload)
    write_md(root / "reports/data_update/v427_provider_failure_classification_latest.md", "# v427 Provider Failure Classification\n\n" + md_table(rows[:100], ["symbol", "provider_symbol", "support_status", "final_failure_reason", "retryable", "recommended_next_action"]))
    _write_csv(root / "results/data_update/v427_provider_failure_classification_latest.csv", rows, ["symbol", "normalized_symbol", "provider_symbol", "akshare_status", "tencent_status", "final_failure_reason", "retryable", "recommended_next_action"])
    return payload


def post_batch_integrity(root: Path, before: dict[str, int], after_payload: dict[str, Any]) -> dict[str, Any]:
    expected = after_payload["latest_expected_trading_day"]
    integrity = post_update_integrity(root, expected)
    after = integrity["latest_date_distribution"]
    before_count = before.get(expected, 0)
    after_count = after.get(expected, 0)
    payload = {
        "task_id": "v427_post_batch_integrity",
        "status": integrity["status"],
        "human_review_required": integrity["human_review_required"],
        "corrupted_csv_files": integrity["corrupted_csv_files"],
        "unreadable_csv_files": integrity["unreadable_csv_files"],
        "row_count_mismatch_files": integrity["row_count_mismatch_files"],
        "latest_date_distribution_before": before,
        "latest_date_distribution_after": after,
        "updated_to_expected_count_before": before_count,
        "updated_to_expected_count_after": after_count,
        "still_stale_count_before": max(0, sum(before.values()) - before_count),
        "still_stale_count_after": max(0, sum(after.values()) - after_count),
        "coverage_delta": after_count - before_count,
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v427_post_batch_integrity_latest.json", payload)
    write_md(root / "reports/data_update/v427_post_batch_integrity_latest.md", "# v427 Post-Batch Integrity\n\n" + md_table([payload], ["status", "updated_to_expected_count_before", "updated_to_expected_count_after", "still_stale_count_before", "still_stale_count_after", "coverage_delta", "human_review_required"]))
    _write_csv(root / "results/data_update/v427_post_batch_integrity_latest.csv", [payload], ["status", "updated_to_expected_count_before", "updated_to_expected_count_after", "still_stale_count_before", "still_stale_count_after", "coverage_delta", "human_review_required"])
    return payload


def write_batch_status(root: Path, execution: dict[str, Any], integrity: dict[str, Any], refresh: dict[str, Any]) -> dict[str, Any]:
    status = build_coverage_status(root)
    status.update(
        {
            "task_id": "v427_batch_update_status",
            "updated_to_expected_count_before": integrity.get("updated_to_expected_count_before"),
            "updated_to_expected_count_after": integrity.get("updated_to_expected_count_after"),
            "still_stale_count_before": integrity.get("still_stale_count_before"),
            "still_stale_count_after": integrity.get("still_stale_count_after"),
            "batch_symbols_attempted": execution.get("symbols_attempted"),
            "batch_symbols_updated": execution.get("symbols_updated"),
            "batch_rows_written": execution.get("rows_written"),
            "research_refresh_success": refresh.get("refresh_success"),
        }
    )
    write_json(root / "results/data_update/v427_batch_update_status_latest.json", status)
    write_md(root / "reports/data_update/v427_batch_update_status_latest.md", "# v427 Batch Update Status\n\n" + md_table([status], ["latest_expected_trading_day", "updated_to_expected_count_before", "updated_to_expected_count_after", "still_stale_count_before", "still_stale_count_after", "batch_symbols_attempted", "batch_symbols_updated", "batch_rows_written", "tencent_fallback_success_count"]))
    return status


def run_batch_catchup(root: Path, *, network: bool = False, max_symbols: int = 300, bucket: str = "P0,P1", provider: str = "auto", resume: bool = False, strict: bool = False, dry_run: bool = False, sleep_between_symbols: float = 0.0) -> dict[str, Any]:
    root = root.resolve()
    before = latest_date_distribution(root)
    bucket_set = None if bucket == "all" else {part.strip() for part in bucket.split(",") if part.strip()}
    buckets = build_stale_buckets(root, max_symbols=max_symbols, buckets=bucket_set)
    symbols = [row["symbol"] for row in buckets["buckets"]]
    normalization = write_symbol_normalization(root, symbols)
    provider_name = "akshare" if provider == "auto" else provider
    result = run_full_market_catchup(root, network=network, max_symbols=max_symbols, priority_only=False, resume=resume, strict=strict, dry_run=dry_run, provider=provider_name, sleep_between_symbols=sleep_between_symbols)
    execution = result["execution"]
    classification = classify_execution_failures(root, execution.get("records", []))
    replay = build_replay_queue(root, classification["classifications"], bucket=bucket, max_symbols=max_symbols)
    coverage_after = build_coverage_status(root)
    integrity = post_batch_integrity(root, before, coverage_after)
    refresh = {"refresh_attempted": False, "refresh_success": False, "failed_steps": [], "candidate_count": 0, "sector_count": 0, "leaderboard_rows": 0}
    if execution.get("rows_written", 0) > 0:
        refresh = refresh_research_outputs(root, reason="v427_batch_catchup", network=False, max_symbols=max_symbols)
    refresh_payload = {"task_id": "v427_research_refresh", "status": "completed" if refresh.get("refresh_success") else "completed_with_warnings", "refresh_attempted": execution.get("rows_written", 0) > 0, "refresh_success": bool(refresh.get("refresh_success")), "failed_steps": refresh.get("failed_steps", []), "stock_candidates_count": refresh.get("candidate_count", 0), "sector_candidates_count": refresh.get("sector_count", 0), "leaderboard_rows": refresh.get("leaderboard_rows", 0), "dashboard_cards_count": 1 if refresh.get("dashboard_cards_status") == "completed" else 0, **boundary_payload()}
    write_json(root / "results/data_update/v427_research_refresh_latest.json", refresh_payload)
    write_md(root / "reports/data_update/v427_research_refresh_latest.md", "# v427 Research Refresh\n\n" + md_table([refresh_payload], ["status", "refresh_attempted", "refresh_success", "stock_candidates_count", "sector_candidates_count", "leaderboard_rows"]))
    batch_id = "v427_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "task_id": "v427_batch_catchup_execution",
        "batch_id": batch_id,
        "status": execution.get("status"),
        "bucket": bucket,
        "symbols_requested": len(symbols),
        "symbols_attempted": execution.get("symbols_attempted"),
        "symbols_updated": execution.get("symbols_updated"),
        "rows_written": execution.get("rows_written"),
        "rows_skipped": execution.get("rows_skipped"),
        "akshare_success": execution.get("provider_akshare_success"),
        "tencent_success": execution.get("provider_tencent_success"),
        "both_failed": execution.get("provider_both_failed"),
        "unsupported_market_count": sum(1 for row in normalization["symbols"] if row["support_status"].startswith("unsupported")),
        "date_missing_count": classification["reason_counts"].get("tencent_date_missing", 0),
        "schema_failed_count": execution.get("schema_failed"),
        "elapsed_seconds": execution.get("elapsed_seconds"),
        "coverage_before": integrity.get("updated_to_expected_count_before"),
        "coverage_after": integrity.get("updated_to_expected_count_after"),
        "still_stale_before": integrity.get("still_stale_count_before"),
        "still_stale_after": integrity.get("still_stale_count_after"),
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v427_batch_catchup_execution_latest.json", payload)
    write_md(root / "reports/data_update/v427_batch_catchup_execution_latest.md", "# v427 Batch Catch-Up Execution\n\n" + md_table([payload], ["status", "bucket", "symbols_requested", "symbols_attempted", "symbols_updated", "rows_written", "akshare_success", "tencent_success", "both_failed", "coverage_before", "coverage_after", "still_stale_before", "still_stale_after"]))
    _write_csv(root / "results/data_update/v427_batch_catchup_execution_latest.csv", [payload], ["batch_id", "bucket", "symbols_requested", "symbols_attempted", "symbols_updated", "rows_written", "rows_skipped", "akshare_success", "tencent_success", "both_failed", "unsupported_market_count", "date_missing_count", "schema_failed_count", "elapsed_seconds", "coverage_before", "coverage_after", "still_stale_before", "still_stale_after"])
    status_payload = write_batch_status(root, payload, integrity, refresh_payload)
    return {"buckets": buckets, "normalization": normalization, "execution": payload, "classification": classification, "replay": replay, "integrity": integrity, "refresh": refresh_payload, "status": status_payload}


def write_review_pack(root: Path) -> dict[str, Any]:
    files = [
        "results/data_update/v427_preflight_latest.json",
        "reports/data_update/v427_preflight_latest.md",
        "results/data_update/v427_symbol_normalization_latest.json",
        "reports/data_update/v427_symbol_normalization_latest.md",
        "results/data_update/v427_symbol_normalization_latest.csv",
        "results/data_update/v427_provider_failure_classification_latest.json",
        "reports/data_update/v427_provider_failure_classification_latest.md",
        "results/data_update/v427_provider_failure_classification_latest.csv",
        "results/data_update/v427_stale_symbol_buckets_latest.json",
        "reports/data_update/v427_stale_symbol_buckets_latest.md",
        "results/data_update/v427_stale_symbol_buckets_latest.csv",
        "results/data_update/v427_batch_catchup_execution_latest.json",
        "reports/data_update/v427_batch_catchup_execution_latest.md",
        "results/data_update/v427_batch_catchup_execution_latest.csv",
        "results/data_update/v427_failed_symbol_replay_queue_latest.json",
        "reports/data_update/v427_failed_symbol_replay_queue_latest.md",
        "results/data_update/v427_failed_symbol_replay_queue_latest.csv",
        "results/data_update/v427_post_batch_integrity_latest.json",
        "reports/data_update/v427_post_batch_integrity_latest.md",
        "results/data_update/v427_post_batch_integrity_latest.csv",
        "results/data_update/v427_research_refresh_latest.json",
        "reports/data_update/v427_research_refresh_latest.md",
        "results/data_update/v427_batch_update_status_latest.json",
        "reports/data_update/v427_batch_update_status_latest.md",
        "results/data_update/v427_gate_integration_latest.json",
        "reports/data_update/v427_gate_integration_latest.md",
        "results/data_update/v427_validation_latest.json",
        "reports/data_update/v427_validation_latest.md",
        "results/data_update/v427_provider_coverage_batch_catchup_handoff_latest.json",
        "reports/data_update/v427_provider_coverage_batch_catchup_handoff_latest.md",
        "agent_workspace/next_codex_task_after_v427_provider_coverage_batch_catchup.md",
        "docs/v427_provider_coverage_expansion.md",
    ]
    readme = root / "dist/quantdesk_v4.2.7_provider_coverage_batch_catchup_review_README.md"
    manifest_path = root / "dist/quantdesk_v4.2.7_provider_coverage_batch_catchup_review_manifest.json"
    zip_path = root / "dist/quantdesk_v4.2.7_provider_coverage_batch_catchup_review_pack.zip"
    write_md(readme, "# QuantDesk v4.2.7 Provider Coverage Batch Catch-Up Review Pack\n\nResearch-only. Not a release. No broker, live trading, auto execution, buy/sell advice, target price, position advice, or real-money allocation.\n")
    files.extend(["dist/quantdesk_v4.2.7_provider_coverage_batch_catchup_review_manifest.json", "dist/quantdesk_v4.2.7_provider_coverage_batch_catchup_review_README.md"])
    manifest = {"pack_name": "quantdesk_v4.2.7_provider_coverage_batch_catchup_review_pack", "status": "completed_with_warnings", "missing_files": [], "included_files": [], "file_count": 0, "zip_size_bytes": 0, "not_a_release": True, "candidate_upgrade_allowed": False, **boundary_payload()}
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
