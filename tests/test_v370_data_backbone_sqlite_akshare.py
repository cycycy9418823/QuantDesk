from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from quantdesk.data_store.hash_utils import DAILY_HASH_FIELDS, compute_row_hash, normalize_hash_value
from quantdesk.data_store.intraday_parquet import build_intraday_parquet_path
from quantdesk.data_store.query import parse_safe_where, run_safe_query
from quantdesk.data_store.repositories import (
    DailyBarRepository,
    DataQualityRepository,
    FundamentalRepository,
    IndexRepository,
    SectorRepository,
)
from quantdesk.data_store.schema import get_required_indexes, get_required_tables
from quantdesk.data_store.sqlite_store import QuantDeskDB
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

ROOT = Path(__file__).resolve().parents[1]


class DataBackboneCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "data" / "quantdesk.db"
        self.db = QuantDeskDB(self.db_path)
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_001_versions(self):
        # Expected: v3.7.0 short app version is centralized.
        self.assertIn(PACKAGE_VERSION, {"3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(APP_VERSION, {"3.7.0-data-backbone", "3.7.0.1-strategy-viability-on-data-backbone", "3.7.0.2-strategy-pipeline-repair", "3.7.0.3-low-price-pipeline-finalization", "3.7.0.4-capital-lot-sensitivity-and-git-hygiene", "3.7.0.5-git-deletion-reconciliation", "3.7.0.6-strategy-regression-guard-and-report-hygiene"})
        self.assertIn(API_SCHEMA_VERSION, {"3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})

    def test_002_init_creates_db(self):
        # Expected: temp data/quantdesk.db exists after initialize.
        self.assertTrue(self.db_path.exists())

    def test_003_required_tables_exist(self):
        # Expected: all required v3.7.0 tables are present.
        names = {row["name"] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        self.assertTrue(set(get_required_tables()).issubset(names))

    def test_004_daily_three_adjustment_tables_exist(self):
        # Expected: forward/back/raw tables all exist separately.
        names = {row["name"] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("daily_bars_forward_adj", names)
        self.assertIn("daily_bars_back_adj", names)
        self.assertIn("daily_bars_raw", names)

    def test_005_daily_view_points_to_forward(self):
        # Expected: daily_bars view returns forward rows only.
        repo = DailyBarRepository(self.db)
        repo.upsert_daily_bars([{"symbol": "600000", "date": "2026-05-27", "close": 10}], "forward")
        repo.upsert_daily_bars([{"symbol": "600000", "date": "2026-05-27", "close": 20}], "raw")
        rows = self.db.execute("SELECT close, adjustment FROM daily_bars WHERE symbol='600000'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 10)
        self.assertEqual(rows[0]["adjustment"], "forward")

    def test_006_partial_daily_is_separate(self):
        # Expected: intraday_partial_daily exists and daily view does not reference it.
        row = self.db.execute_one("SELECT sql FROM sqlite_master WHERE type='view' AND name='daily_bars'")
        self.assertNotIn("intraday_partial_daily", row["sql"])

    def test_007_wal_enabled(self):
        # Expected: PRAGMA journal_mode is wal for initialized writable DB.
        row = self.db.execute_one("PRAGMA journal_mode")
        self.assertEqual(row["journal_mode"], "wal")

    def test_008_busy_timeout(self):
        # Expected: busy_timeout is at least 30000 ms.
        row = self.db.execute_one("PRAGMA busy_timeout")
        self.assertGreaterEqual(row["timeout"], 30000)

    def test_009_required_indexes_exist(self):
        # Expected: symbol/date indexes exist.
        names = {row["name"] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue(set(get_required_indexes()).issubset(names))

    def test_010_upsert_query_daily_forward(self):
        # Expected: inserted forward row can be queried with adjustment=forward.
        repo = DailyBarRepository(self.db)
        repo.upsert_daily_bars([{"symbol": "600000", "date": "20260527", "close": 10}], "forward")
        self.assertEqual(repo.query_daily_bars("600000", adjustment="forward")[0]["close"], 10)

    def test_011_upsert_query_daily_back(self):
        # Expected: back row is stored separately from forward.
        repo = DailyBarRepository(self.db)
        repo.upsert_daily_bars([{"symbol": "600000", "date": "2026-05-27", "close": 11}], "back")
        self.assertEqual(repo.query_daily_bars("600000", adjustment="back")[0]["close"], 11)
        self.assertEqual(repo.query_daily_bars("600000", adjustment="forward"), [])

    def test_012_upsert_query_daily_raw(self):
        # Expected: raw row is stored separately from forward/back.
        repo = DailyBarRepository(self.db)
        repo.upsert_daily_bars([{"symbol": "600000", "date": "2026-05-27", "close": 12}], "raw")
        self.assertEqual(repo.query_daily_bars("600000", adjustment="raw")[0]["close"], 12)

    def test_013_adjustment_not_mixed(self):
        # Expected: querying forward does not return raw/back data.
        repo = DailyBarRepository(self.db)
        repo.upsert_daily_bars([{"symbol": "600000", "date": "2026-05-27", "close": 1}], "forward")
        repo.upsert_daily_bars([{"symbol": "600000", "date": "2026-05-27", "close": 2}], "back")
        repo.upsert_daily_bars([{"symbol": "600000", "date": "2026-05-27", "close": 3}], "raw")
        self.assertEqual(repo.query_daily_bars("600000", adjustment="forward")[0]["close"], 1)

    def test_014_query_cross_section(self):
        # Expected: date query returns multiple symbols.
        repo = DailyBarRepository(self.db)
        repo.upsert_daily_bars([{"symbol": "600000", "date": "2026-05-27", "close": 1}, {"symbol": "000001", "date": "2026-05-27", "close": 2}], "forward")
        self.assertEqual(len(repo.query_cross_section("2026-05-27", adjustment="forward")), 2)

    def test_015_fundamentals_core_fields(self):
        # Expected: core fields and raw_json are preserved.
        repo = FundamentalRepository(self.db)
        repo.upsert_fundamentals([{"symbol": "600000", "report_date": "2026-03-31", "revenue": 1, "net_profit": 2, "equity": 3, "eps": 0.1, "roe": 0.2, "pe": 10, "pb": 1, "raw_json": {"a": 1}}])
        row = repo.latest_fundamentals("600000")
        self.assertEqual(row["revenue"], 1)
        self.assertIn('"a": 1', row["raw_json"])

    def test_016_sector_repository_members(self):
        # Expected: get_members returns expected symbols.
        repo = SectorRepository(self.db)
        repo.upsert_sector_mappings([{"symbol": "600000", "sector_name": "Bank", "sector_type": "industry"}])
        self.assertEqual(repo.get_members("Bank")[0]["symbol"], "600000")

    def test_017_index_daily_repository(self):
        # Expected: index rows query by symbol/date.
        repo = IndexRepository(self.db)
        repo.upsert_index_daily([{"symbol": "000300.SH", "date": "2026-05-27", "close": 4000}])
        self.assertEqual(repo.query_index_daily("000300.SH")[0]["close"], 4000)

    def test_018_data_quality_repository(self):
        # Expected: issue insert/list/mark works.
        repo = DataQualityRepository(self.db)
        repo.insert_issues([{"issue_id": "i1", "issue_type": "missing", "severity": "watch"}])
        self.assertEqual(len(repo.list_open_issues()), 1)
        repo.mark_repaired("i1")
        self.assertEqual(repo.list_open_issues(), [])

    def test_019_row_hash_stable(self):
        # Expected: same normalized row gives same hash.
        row = {"symbol": "600000", "date": "2026-05-27", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 100, "amount": 200}
        self.assertEqual(compute_row_hash(row, DAILY_HASH_FIELDS), compute_row_hash(dict(row), DAILY_HASH_FIELDS))

    def test_020_row_hash_float_precision(self):
        # Expected: float normalization is stable to 6 decimals.
        self.assertEqual(normalize_hash_value(1.0), "1.000000")
        self.assertEqual(normalize_hash_value(1.0000001), "1.000000")

    def test_021_safe_where_allowed(self):
        # Expected: close > 10 compiles to parameterized SQL.
        sql, params = parse_safe_where("close > 10", "daily")
        self.assertEqual(sql, "close > ?")
        self.assertEqual(params, [10.0])

    def test_022_safe_where_rejects_forbidden_field(self):
        # Expected: unknown field is rejected.
        with self.assertRaises(ValueError):
            parse_safe_where("evil > 10", "daily")

    def test_023_safe_where_rejects_sql_injection(self):
        # Expected: ; DROP TABLE is rejected.
        with self.assertRaises(ValueError):
            parse_safe_where("close > 10; DROP TABLE daily_bars_forward_adj", "daily")

    def test_024_safe_query_limit_default(self):
        # Expected: safe query returns a valid payload with default limit.
        payload = run_safe_query("daily", db_path=str(self.db_path))
        self.assertEqual(payload["task_type"], "data_query")
        self.assertEqual(payload["status"], "passed")

    def test_025_data_db_reset_requires_confirm(self):
        # Expected: confirm false refuses.
        result = self.db.reset(confirm=False)
        self.assertFalse(result["ok"])
        self.assertTrue(self.db_path.exists())

    def test_026_data_db_reset_deletes_db_not_csv(self):
        # Expected: db removed and CSV remains.
        csv_path = self.root / "data" / "sample.csv"
        csv_path.write_text("date,close\n2026-05-27,1\n", encoding="utf-8")
        result = self.db.reset(confirm=True)
        self.assertTrue(result["ok"])
        self.assertTrue(csv_path.exists())

    def test_027_parquet_symbol_month_path(self):
        # Expected: data/intraday_parquet/YYYY-MM/<symbol>_<period>.parquet.
        path = build_intraday_parquet_path("600000", "2026-05", "1m")
        self.assertEqual(path.as_posix(), "data/intraday_parquet/2026-05/600000_1m.parquet")

    def test_028_parquet_market_month_path(self):
        # Expected: optional market partition path works.
        path = build_intraday_parquet_path("600000", "2026-05", "1m", partition_mode="market_month")
        self.assertEqual(path.name, "cn_1m.parquet")

    def test_029_qd_data_db_init_json(self):
        # Expected: qd data-db init emits valid JSON summary.
        out = subprocess.check_output([sys.executable, "scripts/qd.py", "data-db", "init", "--db-path", str(self.db_path), "--json", "true", "--summary", "true"], cwd=ROOT, text=True)
        self.assertEqual(json.loads(out)["task_type"], "data_db_init")

    def test_030_qd_data_query_daily_json(self):
        # Expected: qd data-query daily emits valid JSON.
        out = subprocess.check_output([sys.executable, "scripts/qd.py", "data-query", "daily", "--db-path", str(self.db_path), "--symbol", "600000", "--json", "true", "--summary", "true"], cwd=ROOT, text=True)
        self.assertEqual(json.loads(out)["task_type"], "data_query")


if __name__ == "__main__":
    unittest.main()


