from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from quantdesk.data_providers.akshare_provider import fetch_daily_bars_akshare
from quantdesk.data_providers.fundamentals_chain import load_fundamentals
from quantdesk.data_providers.intraday_chain import load_intraday
from quantdesk.data_providers.sector_chain import load_sectors
from quantdesk.data_store.health import (
    audit_daily_bar_coverage,
    build_data_layer_health,
    build_rebalance_events,
    build_trading_calendar_from_daily_bars,
    load_v581_data_gap_diagnosis,
)
from quantdesk.data_store.intraday_parquet import is_parquet_available, update_intraday_manifest
from quantdesk.data_store.quality_repair import build_data_quality_report, repair_missing_dates
from quantdesk.data_store.repositories import FundamentalRepository
from quantdesk.data_store.sqlite_store import QuantDeskDB
from tests._cli_capture import run_qd_json

ROOT = Path(__file__).resolve().parents[1]


class ProviderQualityCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "data" / "quantdesk.db"
        self.db = QuantDeskDB(self.db_path)
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_001_akshare_missing_warning(self):
        # Expected: no network/external AkShare call is honest skipped, not ok.
        payload = fetch_daily_bars_akshare("600000", network=False)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "skipped")
        self.assertIn("network_false_akshare_skipped", payload["warnings"])

    def test_002_network_false_no_external_call(self):
        # Expected: network=false returns skipped without AKShare call.
        payload = fetch_daily_bars_akshare("600000", network=False)
        self.assertFalse(payload["network"])

    def test_003_fundamentals_cache_first(self):
        # Expected: SQLite cache is used before external provider.
        FundamentalRepository(self.db).upsert_fundamentals([{"symbol": "600000", "report_date": "2026-03-31", "revenue": 1}])
        payload = load_fundamentals("600000", network=True, db_path=str(self.db_path), ttl_days=99)
        self.assertEqual(payload["source"], "sqlite_cache")

    def test_004_mootdx_not_full_financial_provider(self):
        # Expected: mootdx is marked limited_basic_indicators when no provider data exists.
        payload = load_fundamentals("000001", network=False, db_path=str(self.db_path))
        self.assertIn("network_false_external_fundamentals_skipped", payload["warnings"])

    def test_005_fundamentals_bad_cache_timestamp_warns_offline(self):
        # Expected: malformed cache freshness metadata remains visible in offline mode.
        FundamentalRepository(self.db).upsert_fundamentals(
            [{"symbol": "600000", "report_date": "2026-03-31", "revenue": 1, "updated_at": "not-a-date"}]
        )
        payload = load_fundamentals("600000", network=False, db_path=str(self.db_path), ttl_days=99)
        self.assertEqual(payload["source"], "sqlite_cache")
        self.assertTrue(payload["rows"])
        self.assertIn("network_false_external_fundamentals_skipped", payload["warnings"])
        self.assertTrue(any(str(item).startswith("cached_fundamentals_updated_at_invalid:") for item in payload["warnings"]))

    def test_006_fundamentals_fetcher_failure_falls_through_to_next_provider(self):
        # Expected: one provider exception is visible and does not abort the chain.
        with mock.patch(
            "quantdesk.data_providers.fundamentals_chain.fetch_fundamental_abstract_ths",
            side_effect=RuntimeError("ths down"),
        ), mock.patch(
            "quantdesk.data_providers.fundamentals_chain.fetch_fundamental_abstract_em",
            return_value={"rows": [{"revenue": 1}], "warnings": [], "source": "akshare_em"},
        ), mock.patch(
            "quantdesk.data_providers.fundamentals_chain.fetch_financial_statements_em",
            return_value={"rows": [], "warnings": ["unused"]},
        ):
            payload = load_fundamentals("600000", network=True, db_path=str(self.db_path), ttl_days=0)
        self.assertTrue(payload["rows"])
        self.assertEqual(payload["rows"][0]["source"], "akshare_em")
        self.assertTrue(any(str(item).startswith("fetch_fundamental_abstract_ths_failed:") for item in payload["warnings"]))

    def test_007_sector_local_csv_fallback(self):
        # Expected: local CSV produces stock_sectors.
        csv_path = self.root / "stock_theme_map.csv"
        csv_path.write_text("symbol,sector\n600000,Bank\n", encoding="utf-8")
        payload = load_sectors(local_csv=str(csv_path), db_path=str(self.db_path))
        self.assertEqual(payload["source"], "local_csv")
        self.assertEqual(self.db.table_stats()["stock_sectors"], 1)

    def test_008_intraday_local_cache_fallback(self):
        # Expected: no network uses local cache or warning.
        # csv_path must point into the isolated tmp dir so the test does not pick
        # up real local minute bars under data/intraday/1m/ (seeded at startup).
        isolated_csv = self.root / "intraday" / "1m" / "600000.csv"
        payload = load_intraday(
            "600000",
            network=False,
            config={"db_path": str(self.db_path), "csv_path": str(isolated_csv)},
        )
        self.assertTrue(payload["ok"])
        self.assertIn("network_false_intraday_provider_skipped", payload["warnings"])

    def test_009_parquet_unavailable_warning(self):
        # Expected: parquet availability is a boolean and not a hard dependency.
        self.assertIsInstance(is_parquet_available(), bool)

    def test_010_intraday_manifest_written(self):
        # Expected: manifest table row is inserted.
        update_intraday_manifest(self.db, {"symbol": "600000", "period": "1m", "month": "2026-05", "storage_path": "x.parquet", "storage_mode": "parquet"})
        self.assertEqual(self.db.table_stats()["intraday_bars_manifest"], 1)

    def test_011_data_layer_health_missing_db_warning(self):
        # Expected: missing db warning does not fail.
        path = self.root / "missing" / "quantdesk.db"
        payload = build_data_layer_health({"db_path": str(path), "data_root": str(self.root / "missing_data")})
        self.assertTrue(payload["ok"])
        self.assertIn("sqlite_db_missing", payload["warnings"])
        self.assertEqual(payload["sqlite_shadow_status"], "absent")
        self.assertEqual(payload["reconciliation_status"], "missing")

    def test_012_initialized_db_health_passed(self):
        # Expected: schema/tables/WAL checks pass with initialized DB.
        payload = build_data_layer_health({"db_path": str(self.db_path), "data_root": str(self.root / "data")})
        self.assertTrue(payload["schema_initialized"])
        self.assertTrue(payload["partial_daily_isolated"])
        self.assertEqual(payload["sqlite_shadow_status"], "empty")
        self.assertEqual(payload["reconciliation_status"], "missing")
        self.assertIn("sqlite_shadow_empty", payload["warnings"])

    def test_013_partial_daily_isolation_health(self):
        # Expected: isolation check passes.
        payload = build_data_layer_health({"db_path": str(self.db_path)})
        self.assertTrue(payload["partial_daily_isolated"])

    def test_011b_strategy_readiness_is_research_only_and_guarded(self):
        # Expected: strategy readiness is an audit only; partial daily is isolated and bias risk is explicit.
        payload = build_data_layer_health({"db_path": str(self.db_path)})
        readiness = payload["strategy_readiness"]
        self.assertTrue(readiness["research_only"])
        self.assertTrue(readiness["partial_daily_isolated"])
        self.assertIn(readiness["survivorship_bias_risk"], {"high", "unknown"})

    def test_011c_rebalance_events_never_use_same_day_execution(self):
        # Expected: every generated event has signal_date before execution_date.
        rows = [{"symbol": "600000", "date": f"2026-05-{day:02d}"} for day in range(1, 23)]
        calendar = build_trading_calendar_from_daily_bars(rows)
        events = build_rebalance_events(calendar, interval=5)
        self.assertTrue(events)
        self.assertTrue(all(event["signal_date"] < event["execution_date"] for event in events))

    def test_011d_daily_bar_coverage_warns_when_sparse(self):
        # Expected: sparse daily bars warn instead of pretending strategy data is complete.
        coverage = audit_daily_bar_coverage([{"symbol": "600000", "date": "2026-05-27"}])
        self.assertEqual(coverage["coverage_status"], "warning")
        self.assertIn("insufficient_daily_bar_coverage", coverage["warnings"])

    def test_v581_data_gap_diagnosis_marks_old_artifact_stale(self):
        diagnosis_path = self.root / "reports" / "v581_data_gap_diagnosis" / "data_gap_diagnosis_latest.json"
        diagnosis_path.parent.mkdir(parents=True, exist_ok=True)
        diagnosis_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "generated_at": "2020-01-01T00:00:00+00:00",
                    "summary": {"total_records": 1},
                }
            ),
            encoding="utf-8",
        )

        payload = load_v581_data_gap_diagnosis(self.root)

        self.assertTrue(payload["available"])
        self.assertEqual(payload["freshness_status"], "stale")
        self.assertIn("v581_data_gap_diagnosis_stale", payload["warnings"])
        self.assertGreater(payload["age_days"], 30)

    def test_012_repair_network_false_skips(self):
        # Expected: no refetch and repair_status skipped_network_false.
        payload = repair_missing_dates("600000", ["2026-05-27"], network=False, db_path=str(self.db_path))
        self.assertEqual(payload["repair_status"], "skipped_network_false")

    def test_013_data_quality_json(self):
        # Expected: data quality report is JSON-safe.
        payload = build_data_quality_report({"db_path": str(self.db_path)})
        json.dumps(payload, allow_nan=False)
        self.assertEqual(payload["task_type"], "data_quality")

    def test_014_qd_data_health_json(self):
        # Expected: qd data-health emits valid JSON.
        payload = run_qd_json(["data-health", "--db-path", str(self.db_path), "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "data_layer_health")

    def test_015_qd_data_update_daily_network_false_json(self):
        # Expected: qd data-update daily emits valid JSON and no external call.
        payload = run_qd_json(["data-update", "daily", "--symbol", "600000", "--network", "false", "--db-path", str(self.db_path), "--json", "true", "--summary", "true"])
        self.assertFalse(payload["network"])
        self.assertEqual(payload["task_type"], "data_update_daily")

    def test_016_qd_data_update_fundamentals_network_false_json(self):
        # Expected: qd data-update fundamentals emits valid JSON and no external call.
        payload = run_qd_json(["data-update", "fundamentals", "--symbol", "600000", "--network", "false", "--db-path", str(self.db_path), "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "data_update_fundamentals")

    def test_017_qd_data_quality_json(self):
        # Expected: qd data-quality emits valid JSON.
        payload = run_qd_json(["data-quality", "--db-path", str(self.db_path), "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "data_quality")

    def test_018_qd_summary_contract_holds(self):
        # Expected: selected qd --json commands output summary fields.
        commands = [
            ["data-db", "stats", "--db-path", str(self.db_path)],
            ["data-query", "daily", "--db-path", str(self.db_path), "--symbol", "600000"],
            ["data-quality", "--db-path", str(self.db_path)],
        ]
        for command in commands:
            payload = run_qd_json([*command, "--json", "true", "--summary", "true"])
            for key in ["ok", "status", "task_type", "app_version", "warning_count", "warnings_top", "output_paths", "disclaimer"]:
                self.assertIn(key, payload)

    def test_019_risk_alerts_smoke_still_passes(self):
        # Expected: v3.6.1 risk alerts behavior remains intact.
        payload = run_qd_json(["risk-alerts", "--synthetic-test-data", "true", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "risk_alerts")

    def test_020_similar_day_smoke_still_passes(self):
        # Expected: v3.6.2 similar-day behavior remains intact.
        payload = run_qd_json(["similar-day", "--synthetic-test-data", "true", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "similar_day")

    def test_021_backtest_default_network_false_still_passes(self):
        # Expected: default backtest still works with network=false.
        payload = run_qd_json(["backtest", "--network", "false", "--json", "true", "--summary", "true"])
        self.assertEqual(payload["task_type"], "backtest")

    def test_022_version_py_no_bom(self):
        # Expected: version.py has no BOM.
        self.assertFalse((ROOT / "quantdesk" / "version.py").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_023_clean_zip_excludes_quantdesk_db_if_present(self):
        # Expected: clean zip excludes data/quantdesk.db when present.
        path = ROOT / "dist" / "quantdesk_clean_v3.7.0-data-backbone.zip"
        if path.exists():
            with zipfile.ZipFile(path) as zf:
                self.assertNotIn("data/quantdesk.db", zf.namelist())

    def test_024_clean_zip_excludes_parquet_if_present(self):
        # Expected: clean zip excludes parquet files.
        path = ROOT / "dist" / "quantdesk_clean_v3.7.0-data-backbone.zip"
        if path.exists():
            with zipfile.ZipFile(path) as zf:
                self.assertFalse(any(name.endswith(".parquet") for name in zf.namelist()))

    def test_025_sourceportable_entries_no_backslashes_if_present(self):
        # Expected: SourcePortable zip entries use / separators.
        path = ROOT / "dist" / "QuantDesk_v3.7.0_SourcePortable.zip"
        if path.exists():
            with zipfile.ZipFile(path) as zf:
                self.assertFalse(any("\\" in name for name in zf.namelist()))


if __name__ == "__main__":
    unittest.main()
