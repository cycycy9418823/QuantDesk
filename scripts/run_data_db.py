from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_store.health import build_data_layer_health
from quantdesk.data_store.migration import migrate_csv_to_sqlite
from quantdesk.data_store.sqlite_store import QuantDeskDB
from quantdesk.version import APP_VERSION


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "false").lower() in {"1", "true", "yes", "y"}


def base_payload(task_type: str) -> dict:
    return {
        "ok": True,
        "status": "passed",
        "task_type": task_type,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "app_version": APP_VERSION,
        "warning_count": 0,
        "warnings_top": [],
        "output_paths": {},
        "disclaimer": "SQLite is a shadow data layer. CSV remains the compatibility fallback.",
    }


def write_override(enabled: bool, read_prefer_sqlite: bool = False, dual_write: bool = True) -> Path:
    path = Path("config/local_data_store_override.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "data_store:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        f"  dual_write: {'true' if (dual_write and enabled) else 'false'}\n"
        f"  read_prefer_sqlite: {'true' if read_prefer_sqlite else 'false'}\n",
        encoding="utf-8",
    )
    return path


def run(args: argparse.Namespace) -> dict:
    action = args.action
    db = QuantDeskDB(args.db_path)
    if action == "init":
        result = base_payload("data_db_init")
        init = db.initialize()
        result.update(init)
        result["output_paths"] = {"db_path": args.db_path}
        return result
    if action == "stats":
        db.initialize()
        result = base_payload("data_db_stats")
        result["table_row_counts"] = db.table_stats()
        result["output_paths"] = {"db_path": args.db_path}
        return result
    if action == "migrate":
        return {**base_payload("csv_to_sqlite_migration"), **migrate_csv_to_sqlite(vars(args))}
    if action == "compare":
        cfg = vars(args)
        cfg["compare"] = True
        return {**base_payload("csv_to_sqlite_migration"), **migrate_csv_to_sqlite(cfg)}
    if action == "vacuum":
        db.initialize()
        db.execute("VACUUM")
        result = base_payload("data_db_vacuum")
        result["output_paths"] = {"db_path": args.db_path}
        return result
    if action == "reset":
        result = base_payload("data_db_reset")
        reset = db.reset(confirm=parse_bool(args.confirm))
        result.update(reset)
        result["warning_count"] = 0 if reset.get("ok") else 1
        result["warnings_top"] = [] if reset.get("ok") else ["confirm_required"]
        return result
    if action == "disable":
        path = write_override(False)
        result = base_payload("data_db_disable")
        result["output_paths"] = {"config_override": str(path)}
        result["data_store_enabled"] = False
        return result
    if action == "enable":
        path = write_override(True, read_prefer_sqlite=parse_bool(args.read_prefer_sqlite), dual_write=True)
        result = base_payload("data_db_enable")
        result["output_paths"] = {"config_override": str(path)}
        result["data_store_enabled"] = True
        result["read_prefer_sqlite"] = parse_bool(args.read_prefer_sqlite)
        return result
    if action == "backup":
        result = base_payload("data_db_backup")
        source = Path(args.db_path)
        if not source.exists():
            result.update({"status": "warning", "warning_count": 1, "warnings_top": ["db_missing"]})
            return result
        target = Path("data/cache/backups") / f"quantdesk_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        result["output_paths"] = {"backup_path": str(target)}
        return result
    if action == "health":
        return build_data_layer_health({"db_path": args.db_path})
    result = base_payload("data_db")
    result.update({"ok": False, "status": "failed", "error": f"unknown action {action}"})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "stats", "migrate", "compare", "vacuum", "reset", "disable", "enable", "backup", "health"])
    parser.add_argument("--db-path", default="data/quantdesk.db")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--adjustment", default="forward")
    parser.add_argument("--dry-run", nargs="?", const="true", default="false")
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--full", nargs="?", const="true", default="false")
    parser.add_argument("--compare", nargs="?", const="true", default="false")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--confirm", nargs="?", const="true", default="false")
    parser.add_argument("--read-prefer-sqlite", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    args.dry_run = parse_bool(args.dry_run)
    args.full = parse_bool(args.full)
    args.compare = parse_bool(args.compare)
    payload = run(args)
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
