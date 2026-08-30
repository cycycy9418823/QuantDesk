from __future__ import annotations

import csv
import os
import shutil
import time
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.bounded_market_data_provider import fetch_bounded_daily_row, provider_config
from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.data_update_staging import staging_dir
from quantdesk.data_update_validation import read_last_date, validate_daily_rows, validate_file_no_mismatch
from quantdesk.market_session_detector import detect_market_session
from quantdesk.provider_coverage_tracker import coverage_record, write_provider_coverage
from quantdesk.research_outputs_refresh import refresh_research_outputs
from quantdesk.strategy_lab.research_signal_safety import boundary_payload
from quantdesk.update_checkpoint import build_checkpoint, new_run_id, write_checkpoint
from quantdesk.update_queue_builder import build_update_queue, fast_last_date, latest_date_distribution


POST_CLOSE_SESSIONS = {"post_close", "after_hours", "before_open"}


def _read_existing(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def _write_symbol(root: Path, symbol: str, expected: str, provider: str, run_id: str, timeout_seconds: int = 30) -> tuple[dict[str, Any], dict[str, Any]]:
    path = root / "data/daily" / f"{symbol}.csv"
    before = read_last_date(path)
    coverage = coverage_record(symbol, expected, provider=provider, network=True, timeout_seconds=timeout_seconds)
    result = {
        "symbol": symbol,
        "latest_before": before,
        "latest_after": before,
        "status": "skipped",
        "rows_written": 0,
        "provider_used": coverage.get("selected_provider", ""),
        "failure_reason": coverage.get("failure_reason", ""),
        "schema_errors": [],
    }
    if not coverage.get("usable_for_write"):
        result["status"] = "provider_failed"
        return result, coverage
    header, existing = _read_existing(path)
    fetched = fetch_bounded_daily_row(symbol, expected, provider=provider, timeout_seconds=timeout_seconds, network=True)
    new_row = {col: "" for col in header}
    new_row.update({key: str(value) for key, value in dict(fetched.get("row") or {}).items() if key in header})
    schema = validate_daily_rows([new_row], expected)
    if schema["status"] != "passed":
        result["status"] = "schema_failed"
        result["schema_errors"] = schema["schema_errors"]
        return result, coverage
    stage = staging_dir(root, f"v426_{provider}_{run_id}", expected)
    stage_file = stage / path.name
    with stage_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(existing + [new_row])
    staged = validate_file_no_mismatch(stage_file)
    if staged["status"] != "passed":
        result["status"] = "staging_validation_failed"
        result["schema_errors"] = staged["schema_errors"]
        return result, coverage
    backup = path.with_suffix(path.suffix + f".v426_backup_{datetime.now().strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    shutil.copy2(stage_file, tmp)
    os.replace(tmp, path)
    readback = validate_file_no_mismatch(path)
    after = read_last_date(path)
    if readback["status"] != "passed" or after != expected:
        result["status"] = "post_write_readback_failed"
        result["schema_errors"] = readback.get("schema_errors", [])
        result["latest_after"] = after
        return result, coverage
    result.update({"status": "updated", "rows_written": 1, "latest_after": after, "backup_file": str(backup.relative_to(root)).replace("\\", "/"), "staging_file": str(stage_file.relative_to(root)).replace("\\", "/")})
    return result, coverage


def post_update_integrity(root: Path, expected: str) -> dict[str, Any]:
    corrupted: list[str] = []
    unreadable: list[str] = []
    row_mismatch: list[str] = []
    distribution = Counter()
    for path in sorted((root / "data/daily").glob("*.csv")):
        try:
            validation = validate_file_no_mismatch(path)
            if validation["status"] != "passed":
                row_mismatch.append(path.name)
            latest = fast_last_date(path)
            distribution[latest or "<blank>"] += 1
        except Exception:
            unreadable.append(path.name)
    updated = distribution.get(expected, 0)
    total = sum(distribution.values())
    payload = {
        "task_id": "v426_post_update_integrity",
        "status": "failed_integrity" if corrupted or unreadable or row_mismatch else "passed",
        "human_review_required": bool(corrupted or unreadable or row_mismatch),
        "corrupted_csv_files": corrupted,
        "unreadable_csv_files": unreadable,
        "row_count_mismatch_files": row_mismatch[:100],
        "latest_date_distribution": dict(sorted(distribution.items(), key=lambda item: item[0])),
        "updated_to_expected_count": updated,
        "still_stale_count": max(0, total - updated),
        "latest_expected_date": expected,
        "data_integrity_status": "clean" if not (corrupted or unreadable or row_mismatch) else "human_review_required",
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v426_post_update_integrity_latest.json", payload)
    write_md(root / "reports/data_update/v426_post_update_integrity_latest.md", "# v426 Post-Update Integrity\n\n" + md_table([payload], ["status", "updated_to_expected_count", "still_stale_count", "latest_expected_date", "data_integrity_status", "human_review_required"]))
    return payload


def write_research_refresh(root: Path, *, rows_written: int, max_symbols: int) -> dict[str, Any]:
    if rows_written > 0:
        refresh = refresh_research_outputs(root, reason="v426_full_market_catchup", network=False, max_symbols=max_symbols)
    else:
        refresh = {"status": "skipped_no_rows_written", "refresh_attempted": False, "refresh_success": False, "failed_steps": [], "candidate_count": 0, "sector_count": 0, "leaderboard_rows": 0}
    payload = {
        "task_id": "v426_research_refresh",
        "status": "completed" if refresh.get("refresh_success") else "completed_with_warnings",
        "refresh_attempted": bool(rows_written > 0),
        "refresh_success": bool(refresh.get("refresh_success")),
        "failed_steps": refresh.get("failed_steps", []),
        "stock_candidates_count": refresh.get("candidate_count", 0),
        "sector_candidates_count": refresh.get("sector_count", 0),
        "leaderboard_rows": refresh.get("leaderboard_rows", 0),
        "dashboard_cards_count": 1 if refresh.get("dashboard_cards_status") == "completed" else 0,
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v426_research_refresh_latest.json", payload)
    write_md(root / "reports/data_update/v426_research_refresh_latest.md", "# v426 Research Refresh\n\n" + md_table([payload], ["status", "refresh_attempted", "refresh_success", "stock_candidates_count", "sector_candidates_count", "leaderboard_rows", "dashboard_cards_count"]))
    return payload


def run_full_market_catchup(
    root: Path,
    *,
    network: bool = False,
    max_symbols: int = 300,
    priority_only: bool = True,
    resume: bool = False,
    start_after: str = "",
    strict: bool = False,
    dry_run: bool = False,
    provider: str = "akshare",
    sleep_between_symbols: float = 0.0,
) -> dict[str, Any]:
    root = root.resolve()
    for rel in ["results/data_update", "reports/data_update", "dist", "agent_workspace"]:
        (root / rel).mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    run_id = new_run_id()
    t0 = time.perf_counter()
    config = provider_config(root, provider_type=provider, max_symbols=max_symbols)
    session = detect_market_session(root)
    expected = session["expected_latest_completed_trade_date"]
    queue_payload = build_update_queue(root, max_symbols=max_symbols, priority_only=priority_only, resume=resume, start_after=start_after, expected_date=expected)
    before_dist = queue_payload["latest_date_distribution"]
    selected = [row for row in queue_payload["queue"] if row.get("update_required")]
    records: list[dict[str, Any]] = []
    coverage_records: list[dict[str, Any]] = []
    completed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    rows_written = 0
    status = "dry_run_no_write" if dry_run or not network else "completed"
    if not network:
        skipped = [row["symbol"] for row in selected]
        coverage_records = [coverage_record(row["symbol"], expected, provider=provider, network=False) for row in selected]
    elif session["session"] not in POST_CLOSE_SESSIONS:
        status = "skipped_session_not_post_close"
        skipped = [row["symbol"] for row in selected]
    elif dry_run:
        skipped = [row["symbol"] for row in selected]
    else:
        for row in selected:
            symbol = row["symbol"]
            result, coverage = _write_symbol(root, symbol, expected, provider, run_id)
            records.append(result)
            coverage_records.append(coverage)
            if result["status"] == "updated":
                completed.append(symbol)
                rows_written += int(result.get("rows_written", 0))
            elif result["status"] == "provider_failed":
                failed.append(symbol)
                if strict:
                    status = "human_review_required"
                    break
            else:
                failed.append(symbol)
                if strict:
                    status = "human_review_required"
                    break
            write_checkpoint(root, build_checkpoint(run_id=run_id, started_at=started_at, last_symbol_processed=symbol, completed_symbols=completed, failed_symbols=failed, skipped_symbols=skipped, rows_written=rows_written, max_symbols=max_symbols, priority_only=priority_only))
            if sleep_between_symbols > 0:
                time.sleep(float(sleep_between_symbols))
        if status != "human_review_required":
            status = "completed_with_warnings" if failed else "completed"
    coverage_payload = write_provider_coverage(root, coverage_records)
    checkpoint = write_checkpoint(root, build_checkpoint(run_id=run_id, started_at=started_at, last_symbol_processed=(records[-1]["symbol"] if records else ""), completed_symbols=completed, failed_symbols=failed, skipped_symbols=skipped, rows_written=rows_written, max_symbols=max_symbols, priority_only=priority_only))
    integrity = post_update_integrity(root, expected)
    refresh = write_research_refresh(root, rows_written=rows_written, max_symbols=max_symbols)
    after_dist = latest_date_distribution(root)
    execution_rows = records or [{"symbol": row["symbol"], "status": "skipped_network_false" if not network else status, "rows_written": 0} for row in selected]
    payload = {
        "task_id": "v426_catchup_execution",
        "status": "human_review_required" if integrity.get("human_review_required") else status,
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "network": bool(network),
        "dry_run": bool(dry_run),
        "session": session["session"],
        "expected_latest_date": expected,
        "symbols_requested": len(queue_payload["queue"]),
        "symbols_attempted": len(selected) if network and not dry_run and session["session"] in POST_CLOSE_SESSIONS else 0,
        "symbols_updated": len(completed),
        "rows_written": rows_written,
        "rows_skipped": len(skipped) + sum(1 for row in queue_payload["queue"] if not row.get("update_required")),
        "provider_akshare_success": coverage_payload["akshare_success"],
        "provider_tencent_success": coverage_payload["tencent_success"],
        "provider_both_failed": coverage_payload["both_failed"],
        "schema_failed": sum(1 for row in records if row.get("status") == "schema_failed"),
        "date_mismatch": sum(1 for row in records if row.get("status") == "post_write_readback_failed"),
        "latest_date_before_distribution": before_dist,
        "latest_date_after_distribution": after_dist,
        "elapsed_seconds": round(time.perf_counter() - t0, 3),
        "records": execution_rows,
        "checkpoint": checkpoint,
        "integrity_status": integrity.get("status"),
        "research_refresh_status": refresh.get("status"),
        "candidate_upgrade_allowed": False,
        "canonical_equity_overwritten": False,
        "generated_trade_advice": False,
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v426_catchup_execution_latest.json", payload)
    write_md(root / "reports/data_update/v426_catchup_execution_latest.md", "# v426 Catch-Up Execution\n\n" + md_table([payload], ["status", "network", "session", "symbols_requested", "symbols_attempted", "symbols_updated", "rows_written", "rows_skipped", "provider_akshare_success", "provider_tencent_success", "provider_both_failed", "elapsed_seconds"]))
    csv_path = root / "results/data_update/v426_catchup_execution_latest.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        cols = ["symbol", "latest_before", "latest_after", "status", "rows_written", "provider_used", "failure_reason"]
        writer = csv.DictWriter(handle, fieldnames=cols)
        writer.writeheader()
        writer.writerows([{col: row.get(col, "") for col in cols} for row in execution_rows])
    return {"config": config, "queue": queue_payload, "coverage": coverage_payload, "execution": payload, "checkpoint": checkpoint, "integrity": integrity, "refresh": refresh}


def write_review_pack(root: Path) -> dict[str, Any]:
    files = [
        "results/data_update/v426_preflight_latest.json",
        "reports/data_update/v426_preflight_latest.md",
        "results/data_update/v426_update_queue_latest.json",
        "reports/data_update/v426_update_queue_latest.md",
        "results/data_update/v426_update_queue_latest.csv",
        "results/data_update/v426_provider_coverage_latest.json",
        "reports/data_update/v426_provider_coverage_latest.md",
        "results/data_update/v426_provider_coverage_latest.csv",
        "results/data_update/v426_catchup_execution_latest.json",
        "reports/data_update/v426_catchup_execution_latest.md",
        "results/data_update/v426_catchup_execution_latest.csv",
        "results/data_update/v426_update_checkpoint_latest.json",
        "reports/data_update/v426_update_checkpoint_latest.md",
        "results/data_update/v426_post_update_integrity_latest.json",
        "reports/data_update/v426_post_update_integrity_latest.md",
        "results/data_update/v426_research_refresh_latest.json",
        "reports/data_update/v426_research_refresh_latest.md",
        "results/data_update/v426_update_coverage_status_latest.json",
        "reports/data_update/v426_update_coverage_status_latest.md",
        "results/data_update/v426_gate_integration_latest.json",
        "reports/data_update/v426_gate_integration_latest.md",
        "results/data_update/v426_validation_latest.json",
        "reports/data_update/v426_validation_latest.md",
        "results/data_update/v426_full_market_daily_catchup_handoff_latest.json",
        "reports/data_update/v426_full_market_daily_catchup_handoff_latest.md",
        "agent_workspace/next_codex_task_after_v426_full_market_daily_catchup.md",
        "docs/v426_full_market_daily_catchup.md",
    ]
    readme = root / "dist/quantdesk_v4.2.6_full_market_daily_catchup_review_README.md"
    manifest_path = root / "dist/quantdesk_v4.2.6_full_market_daily_catchup_review_manifest.json"
    zip_path = root / "dist/quantdesk_v4.2.6_full_market_daily_catchup_review_pack.zip"
    write_md(readme, "# QuantDesk v4.2.6 Full-Market Daily Catch-Up Review Pack\n\nResearch-only. Not a release. No broker, live trading, auto execution, buy/sell advice, target price, position advice, or real-money allocation.\n")
    files.extend(["dist/quantdesk_v4.2.6_full_market_daily_catchup_review_manifest.json", "dist/quantdesk_v4.2.6_full_market_daily_catchup_review_README.md"])
    manifest = {"pack_name": "quantdesk_v4.2.6_full_market_daily_catchup_review_pack", "status": "completed_with_warnings", "missing_files": [], "included_files": [], "file_count": 0, "zip_size_bytes": 0, "not_a_release": True, "candidate_upgrade_allowed": False, **boundary_payload()}
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
