from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quantdesk.data_store.dual_writer import atomic_write_csv, read_daily_prefer_sqlite, write_daily_dual
from quantdesk.data_store.migration import load_daily_csv_rows, migrate_csv_to_sqlite, scan_csv_files
from quantdesk.data_store.repositories import DailyBarRepository, UpdateLogRepository
from quantdesk.data_store.sqlite_store import QuantDeskDB

ROOT = Path(__file__).resolve().parents[1]


class MigrationIntegrityCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.db_path = self.data / "quantdesk.db"
        self.db = QuantDeskDB(self.db_path)
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def write_csv(self, rel: str, rows: list[dict], fieldnames: list[str] | None = None) -> Path:
        path = self.data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = fieldnames or list(rows[0].keys())
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_001_scan_counts_not_hardcoded(self):
        # Expected: counts reflect temp files, not 5640.
        self.write_csv("daily/forward_adjusted/600000.csv", [{"date": "2026-05-27", "close": "1"}])
        counts = scan_csv_files(self.data)
        self.assertEqual(counts["total_csv_count"], 1)
        self.assertEqual(counts["daily_forward_count"], 1)

    def test_002_csv_date_aliases_date(self):
        # Expected: date column is parsed.
        path = self.write_csv("daily/forward_adjusted/600000.csv", [{"date": "2026-05-27", "close": "1"}])
        rows, _ = load_daily_csv_rows(path)
        self.assertEqual(rows[0]["date"], "2026-05-27")

    def test_003_csv_date_aliases_trade_date(self):
        # Expected: trade_date column is parsed.
        path = self.write_csv("daily/forward_adjusted/600000.csv", [{"trade_date": "20260527", "close": "1"}])
        rows, _ = load_daily_csv_rows(path)
        self.assertEqual(rows[0]["date"], "2026-05-27")

    def test_004_csv_date_aliases_chinese(self):
        # Expected: 日期 column is parsed.
        path = self.write_csv("daily/forward_adjusted/600000.csv", [{"日期": "2026/5/27", "收盘": "1"}])
        rows, _ = load_daily_csv_rows(path)
        self.assertEqual(rows[0]["date"], "2026-05-27")

    def test_005_csv_volume_aliases_volume(self):
        # Expected: volume column is parsed.
        path = self.write_csv("daily/forward_adjusted/600000.csv", [{"date": "2026-05-27", "close": "1", "volume": "100"}])
        rows, _ = load_daily_csv_rows(path)
        self.assertEqual(rows[0]["volume"], 100)

    def test_006_csv_volume_aliases_vol(self):
        # Expected: vol column is parsed.
        path = self.write_csv("daily/forward_adjusted/600000.csv", [{"date": "2026-05-27", "close": "1", "vol": "100"}])
        rows, _ = load_daily_csv_rows(path)
        self.assertEqual(rows[0]["volume"], 100)

    def test_007_csv_volume_aliases_chinese(self):
        # Expected: 成交量 column is parsed.
        path = self.write_csv("daily/forward_adjusted/600000.csv", [{"date": "2026-05-27", "close": "1", "成交量": "100"}])
        rows, _ = load_daily_csv_rows(path)
        self.assertEqual(rows[0]["volume"], 100)

    def test_008_missing_close_warning(self):
        # Expected: file skipped with warning, no crash.
        path = self.write_csv("daily/forward_adjusted/600000.csv", [{"date": "2026-05-27", "open": "1"}])
        rows, warnings = load_daily_csv_rows(path)
        self.assertEqual(rows, [])
        self.assertTrue(warnings)

    def test_009_forward_migrates_to_forward_table(self):
        # Expected: rows in daily_bars_forward_adj only.
        self.write_csv("daily/forward_adjusted/600000.csv", [{"date": "2026-05-27", "close": "1"}])
        migrate_csv_to_sqlite({"data_root": str(self.data), "db_path": str(self.db_path), "adjustment": "forward"})
        self.assertEqual(self.db.table_stats()["daily_bars_forward_adj"], 1)
        self.assertEqual(self.db.table_stats()["daily_bars_back_adj"], 0)

    def test_010_back_migrates_to_back_table(self):
        # Expected: rows in daily_bars_back_adj only.
        self.write_csv("daily/back_adjusted/600000.csv", [{"date": "2026-05-27", "close": "1"}])
        migrate_csv_to_sqlite({"data_root": str(self.data), "db_path": str(self.db_path), "adjustment": "back"})
        self.assertEqual(self.db.table_stats()["daily_bars_back_adj"], 1)

    def test_011_raw_migrates_to_raw_table(self):
        # Expected: rows in daily_bars_raw only.
        self.write_csv("daily/raw/600000.csv", [{"date": "2026-05-27", "close": "1"}])
        migrate_csv_to_sqlite({"data_root": str(self.data), "db_path": str(self.db_path), "adjustment": "raw"})
        self.assertEqual(self.db.table_stats()["daily_bars_raw"], 1)

    def test_012_partial_daily_migrates_only_partial_table(self):
        # Expected: partial daily creates no daily_bars rows.
        self.write_csv("intraday/partial_daily/600000.csv", [{"date": "2026-05-27", "close": "1"}])
        migrate_csv_to_sqlite({"data_root": str(self.data), "db_path": str(self.db_path), "adjustment": "forward"})
        stats = self.db.table_stats()
        self.assertEqual(stats["intraday_partial_daily"], 1)
        self.assertEqual(stats["daily_bars_forward_adj"], 0)

    def test_013_compare_hash_match(self):
        # Expected: known CSV and SQLite rows hash match.
        path = self.write_csv("daily/forward_adjusted/600000.csv", [{"date": "2026-05-27", "open": "1", "high": "2", "low": "1", "close": "1.5", "volume": "100", "amount": "200"}])
        migrate_csv_to_sqlite({"data_root": str(self.data), "db_path": str(self.db_path), "adjustment": "forward"})
        compare = DailyBarRepository(self.db).compare_with_csv("600000", "forward", path)
        self.assertEqual(compare["hash_match_ratio"], 1.0)

    def test_014_dry_run_writes_no_rows(self):
        # Expected: dry run leaves table row count zero.
        self.write_csv("daily/forward_adjusted/600000.csv", [{"date": "2026-05-27", "close": "1"}])
        migrate_csv_to_sqlite({"data_root": str(self.data), "db_path": str(self.db_path), "adjustment": "forward", "dry_run": True})
        self.assertEqual(self.db.table_stats()["daily_bars_forward_adj"], 0)

    def test_015_batch_transaction(self):
        # Expected: batch upsert commits all rows.
        rows = [{"symbol": "600000", "date": f"2026-05-{day:02d}", "close": day} for day in range(1, 6)]
        DailyBarRepository(self.db).upsert_daily_bars_batch(rows, "forward", batch_size=2)
        self.assertEqual(self.db.table_stats()["daily_bars_forward_adj"], 5)

    def test_016_dual_write_both_success(self):
        # Expected: csv_written and sqlite_written are true.
        result = write_daily_dual([{"symbol": "600000", "date": "2026-05-27", "close": 1}], self.root / "out.csv", db_path=str(self.db_path))
        self.assertTrue(result["csv_written"])
        self.assertTrue(result["sqlite_written"])

    def test_017_dual_write_sqlite_fail_csv_success_is_fail_closed(self):
        # Round 21: partial dual-write success is fail-closed (ok=False), not
        # warning+ok, because readers consume both stores.
        bad_parent = self.root / "not_a_dir"
        bad_parent.write_text("x", encoding="utf-8")
        result = write_daily_dual([{"symbol": "600000", "date": "2026-05-27", "close": 1}], self.root / "out.csv", db_path=str(bad_parent / "bad.db"))
        self.assertEqual(result["status"], "failed_partial")
        self.assertFalse(result["ok"])
        self.assertTrue(result["retry_recommended"])
        self.assertTrue(result["csv_written"])
        self.assertIn("dual_write_partial_failure_fail_closed", result["errors"])

    def test_018_dual_write_csv_fail_sqlite_success_is_fail_closed(self):
        # Round 21: same fail-closed contract for the mirrored failure side.
        bad_path = self.root / "missing" / "out.csv"
        bad_path.parent.write_text("not a dir", encoding="utf-8")
        result = write_daily_dual([{"symbol": "600000", "date": "2026-05-27", "close": 1}], bad_path, db_path=str(self.db_path))
        self.assertEqual(result["status"], "failed_partial")
        self.assertFalse(result["ok"])
        self.assertTrue(result["sqlite_written"])
        self.assertIn("dual_write_partial_failure_fail_closed", result["errors"])

    def test_019_dual_write_both_fail(self):
        # Expected: both fail returns ok false.
        bad_path = self.root / "missing" / "out.csv"
        bad_path.parent.write_text("not a dir", encoding="utf-8")
        result = write_daily_dual([{"date": "", "close": 1}], bad_path, db_path=str(bad_path.parent / "bad.db"))
        self.assertFalse(result["ok"])

    def test_020_atomic_csv_write_no_partial_file(self):
        # Expected: atomic write creates a complete CSV.
        path = self.root / "atomic.csv"
        atomic_write_csv(path, [{"symbol": "600000", "date": "2026-05-27", "close": 1}])
        self.assertIn("600000", path.read_text(encoding="utf-8"))

    def test_021_read_prefer_csv_default(self):
        # Expected: read_prefer_sqlite=false reads CSV first.
        path = self.write_csv("daily/forward_adjusted/600000.csv", [{"date": "2026-05-27", "close": "2"}])
        rows, meta = read_daily_prefer_sqlite("600000", csv_path=path, config={"db_path": str(self.db_path), "read_prefer_sqlite": False})
        self.assertEqual(meta["read_source"], "csv")
        self.assertEqual(rows[0]["close"], 2)

    def test_022_read_prefer_sqlite_enabled(self):
        # Expected: read_prefer_sqlite=true reads SQLite first.
        DailyBarRepository(self.db).upsert_daily_bars([{"symbol": "600000", "date": "2026-05-27", "close": 3}], "forward")
        rows, meta = read_daily_prefer_sqlite("600000", config={"db_path": str(self.db_path), "read_prefer_sqlite": True})
        self.assertEqual(meta["read_source"], "sqlite")
        self.assertEqual(rows[0]["close"], 3)

    def test_023_read_prefer_sqlite_falls_back_to_csv_when_db_unavailable(self):
        # Expected: CSV fallback remains usable when the SQLite shadow layer cannot open.
        path = self.write_csv("daily/forward_adjusted/600000.csv", [{"date": "2026-05-27", "close": "4"}])
        bad_parent = self.root / "not_a_db_dir"
        bad_parent.write_text("x", encoding="utf-8")
        rows, meta = read_daily_prefer_sqlite(
            "600000",
            csv_path=path,
            config={"db_path": str(bad_parent / "bad.db"), "read_prefer_sqlite": True},
        )
        self.assertEqual(meta["read_source"], "csv")
        self.assertTrue(meta["fallback_used"])
        self.assertEqual(rows[0]["close"], 4)
        self.assertTrue(any(str(item).startswith("sqlite_read_failed:") for item in meta["warnings"]))

    def test_024_data_update_log_written(self):
        # Expected: dual write writes data_update_log.
        write_daily_dual([{"symbol": "600000", "date": "2026-05-27", "close": 1}], self.root / "out.csv", db_path=str(self.db_path))
        self.assertGreaterEqual(len(UpdateLogRepository(self.db).list_updates()), 1)

    def test_025_dual_write_log_failure_is_visible_warning(self):
        # Expected: audit-log failure is visible, not silently swallowed.
        with patch("quantdesk.data_store.dual_writer.UpdateLogRepository") as repo_cls:
            repo_cls.return_value.log_update.side_effect = RuntimeError("log boom")
            result = write_daily_dual(
                [{"symbol": "600000", "date": "2026-05-27", "close": 1}],
                self.root / "out.csv",
                db_path=str(self.db_path),
            )
        self.assertTrue(result["csv_written"])
        self.assertTrue(result["sqlite_written"])
        self.assertEqual(result["status"], "warning")
        self.assertTrue(result["retry_recommended"])
        self.assertIn("update_log_write_failed:log boom", result["warnings"])

    def test_026_qd_data_db_migrate_dry_run_json(self):
        # Expected: qd data-db migrate dry run emits valid JSON.
        out = subprocess.check_output([sys.executable, "scripts/qd.py", "data-db", "migrate", "--db-path", str(self.db_path), "--data-root", str(self.data), "--dry-run", "true", "--json", "true", "--summary", "true"], cwd=ROOT, text=True)
        self.assertTrue(json.loads(out)["dry_run"])

    def test_027_qd_data_db_stats_json(self):
        # Expected: qd data-db stats emits valid JSON.
        out = subprocess.check_output([sys.executable, "scripts/qd.py", "data-db", "stats", "--db-path", str(self.db_path), "--json", "true", "--summary", "true"], cwd=ROOT, text=True)
        self.assertEqual(json.loads(out)["task_type"], "data_db_stats")


if __name__ == "__main__":
    unittest.main()
