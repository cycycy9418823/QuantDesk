from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.stage6_duplicate_session_repair import (  # noqa: E402
    repair_intraday_duplicate_sessions,
    scan_intraday_duplicate_sessions,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the complete canonical 1-minute pool for same-symbol/date "
            "duplicate sessions. Repair is opt-in and transactional."
        )
    )
    parser.add_argument(
        "--canonical-dir",
        default=str(PROJECT_ROOT / "data" / "intraday" / "1m"),
    )
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply only duplicate-session repairs after the full read-only scan.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_id = str(args.run_id or "").strip() or datetime.now(
        timezone.utc
    ).strftime("stage6-section16-%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir or (PROJECT_ROOT / "results" / "data_chain" / "stage6_section16" / f"duplicate_scan_{run_id}"))
    scan = scan_intraday_duplicate_sessions(
        canonical_dir=Path(args.canonical_dir),
        output_dir=output_dir,
        run_id=run_id,
    )
    repair: dict[str, object] | None = None
    if args.apply and scan.get("status") != "failed":
        backup_dir = Path(
            args.backup_dir
            or (output_dir / "backups")
        )
        repair = repair_intraday_duplicate_sessions(
            canonical_dir=Path(args.canonical_dir),
            anomalies=scan.get("anomalies") or [],
            backup_dir=backup_dir,
            output_path=output_dir / "duplicate_repair.json",
            run_id=run_id,
            apply=True,
        )
    summary = {
        "run_id": run_id,
        "scan_status": scan.get("status"),
        "scanned_file_count": scan.get("scanned_file_count"),
        "scanned_file_bytes": scan.get("scanned_file_bytes"),
        "symbol_date_count": scan.get("symbol_date_count"),
        "anomalous_symbol_date_count": scan.get("anomalous_symbol_date_count"),
        "duplicate_pollution_symbol_date_count": scan.get(
            "duplicate_pollution_symbol_date_count"
        ),
        "inventory_csv_path": scan.get("inventory_csv_path"),
        "anomalies_json_path": scan.get("anomalies_json_path"),
        "repair": repair,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    if scan.get("status") == "failed":
        return 1
    if repair and repair.get("status") == "failed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
