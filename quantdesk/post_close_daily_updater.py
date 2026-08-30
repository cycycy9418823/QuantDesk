from __future__ import annotations

from pathlib import Path
from typing import Any

import csv
import inspect
import os
import shutil
from datetime import datetime
from itertools import islice

from quantdesk.bounded_market_data_provider import fetch_bounded_daily_row, is_simulated_source, simulated_daily_row
from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.data_update_staging import staging_dir
from quantdesk.data_update_validation import read_last_date, validate_daily_rows, validate_file_no_mismatch
from quantdesk.market_session_detector import detect_market_session


POST_CLOSE_SESSIONS = {"post_close", "after_hours", "before_open"}


def run_post_close_daily_update(
    root: Path,
    *,
    network: bool = False,
    max_symbols: int = 80,
    provider: str = "bounded_optional_provider",
    dry_run: bool = False,
    write_mode: str = "staging",
    simulate: bool = False,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    session = detect_market_session(root)
    expected = session["expected_latest_completed_trade_date"]
    stage = staging_dir(root, provider, expected)
    attempted = bool(network and session["session"] in POST_CLOSE_SESSIONS)
    schema = validate_daily_rows([], expected)
    failures: list[str] = []
    symbols_attempted = 0
    symbols_updated = 0
    rows_written = 0
    rows_skipped = 0
    latest_before = session["latest_local_trade_date"]
    latest_after = latest_before
    provider_attempts: list[dict[str, Any]] = []
    if not network:
        status = "skipped_network_false"
        failures.append("network=false")
    elif session["session"] not in POST_CLOSE_SESSIONS:
        status = "skipped_session_not_post_close"
        failures.append(f"session={session['session']}")
    elif dry_run:
        status = "dry_run_no_write"
        failures.append("dry_run")
    else:
        limit = max(1, min(int(max_symbols), 80))
        targets = list(islice((root / "data/daily").glob("*.csv"), limit))
        symbols_attempted = len(targets)
        status = "completed"
        for path in targets:
            last_date = read_last_date(path)
            if last_date >= expected:
                rows_skipped += 1
                continue
            header, existing_rows = [], []
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    header = list(reader.fieldnames or [])
                    existing_rows = [dict(row) for row in reader]
                if simulate:
                    new_row = simulated_daily_row(path, expected)
                    provider_attempts.append({"symbol": path.stem, "provider": "v425_simulated_provider", "status": "ok"})
                else:
                    fetch_kwargs: dict[str, Any] = {"provider": provider, "timeout_seconds": timeout_seconds}
                    if "network" in inspect.signature(fetch_bounded_daily_row).parameters:
                        fetch_kwargs["network"] = True
                    fetched = fetch_bounded_daily_row(path.stem, expected, **fetch_kwargs)
                    provider_attempts.append({"symbol": path.stem, **{key: value for key, value in fetched.items() if key != "row"}})
                    if fetched.get("status") != "ok":
                        failures.append(f"{path.name}:provider_failed")
                        status = "provider_failed_fallback_local"
                        continue
                    new_row = {col: "" for col in header}
                    new_row.update({key: str(value) for key, value in dict(fetched.get("row") or {}).items() if key in header})
                if not simulate and is_simulated_source(new_row.get("source")):
                    failures.append(f"{path.name}:refused_synthetic_row_in_real_update")
                    status = "failed_validation"
                    continue
                schema = validate_daily_rows([new_row], expected)
                if schema["status"] != "passed":
                    failures.extend(schema["schema_errors"])
                    status = "failed_validation"
                    continue
                stage_file = stage / path.name
                stage_file.parent.mkdir(parents=True, exist_ok=True)
                with stage_file.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=header)
                    writer.writeheader()
                    writer.writerows(existing_rows + [new_row])
                staged_validation = validate_file_no_mismatch(stage_file)
                if staged_validation["status"] != "passed":
                    failures.extend(staged_validation["schema_errors"])
                    status = "failed_validation"
                    continue
                backup = path.with_suffix(path.suffix + f".v425_backup_{datetime.now().strftime('%Y%m%d%H%M%S')}")
                shutil.copy2(path, backup)
                tmp = path.with_suffix(path.suffix + ".tmp")
                shutil.copy2(stage_file, tmp)
                os.replace(tmp, path)
                readback = validate_file_no_mismatch(path)
                if readback["status"] != "passed" or read_last_date(path) != expected:
                    failures.append(f"post_write_readback_failed:{path.name}")
                    status = "failed_validation"
                    continue
                symbols_updated += 1
                rows_written += 1
                if not simulate:
                    status = "completed_real_provider"
            except Exception as exc:
                failures.append(f"{path.name}:{exc!r}")
                status = "failed_validation"
        latest_after = max([latest_before, expected] if rows_written > 0 else [latest_before])
        if rows_written == 0 and status == "completed":
            status = "completed_no_new_rows"
    payload = {
        "task_id": "v424_post_close_daily_update",
        "status": status,
        "attempted": attempted,
        "network": bool(network),
        "session": session["session"],
        "provider": provider,
        "symbols_attempted": symbols_attempted if symbols_attempted else (min(int(max_symbols), int(max_symbols)) if attempted else 0),
        "symbols_updated": symbols_updated,
        "rows_written": rows_written,
        "rows_skipped": rows_skipped,
        "failures": failures,
        "latest_date_before": latest_before,
        "latest_date_after": latest_after,
        "expected_latest_completed_trade_date": expected,
        "staging_dir": str(stage.relative_to(root)).replace("\\", "/"),
        "protected_paths_status": "unchanged",
        "schema_errors": schema["schema_errors"] if attempted else [],
        "dry_run": bool(dry_run),
        "write_mode": write_mode,
        "simulation": bool(simulate),
        "timeout_seconds": int(timeout_seconds),
        "provider_attempts": provider_attempts,
        "real_provider_write": bool(rows_written > 0 and not simulate),
        "research_only": True,
        "no_broker": True,
        "no_live_trading": True,
        "no_auto_execution": True,
        "no_buy_sell_advice": True,
        "no_real_money_allocation": True,
    }
    write_json(root / "results/data_update/v424_post_close_daily_update_latest.json", payload)
    write_md(root / "reports/data_update/v424_post_close_daily_update_latest.md", "# v424 Post-Close Daily Update\n\n" + md_table([payload], ["status", "attempted", "session", "network", "rows_written", "latest_date_before", "latest_date_after", "protected_paths_status"]))
    return payload
