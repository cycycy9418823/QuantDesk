from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from quantdesk.research.factor_evaluation import PIT_UNIVERSE_WARNING
from quantdesk.research.factor_combiner import build_combined_score, build_combined_score_report
from quantdesk.research.factor_research_dashboard import (
    build_factor_research_dashboard,
    build_synthetic_research_payloads,
)
from quantdesk.research.point_in_time_universe import (
    build_pit_universe_panel,
    load_historical_listings,
    summarize_pit_universe_status,
    universe_at,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_INVENTORY = PROJECT_ROOT / "docs" / "architecture_inventory.md"


def _has_bad_number(value) -> bool:
    if isinstance(value, dict):
        return any(_has_bad_number(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_bad_number(item) for item in value)
    return isinstance(value, float) and (math.isnan(value) or math.isinf(value))


class PointInTimeUniverseLiteTest(unittest.TestCase):
    def test_load_historical_listings_missing_bad_dates_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.csv"
            frame, warnings = load_historical_listings(missing)
            self.assertTrue(frame.empty)
            self.assertTrue(any("missing_historical_listings" in item for item in warnings))

            csv_path = root / "historical_listings.csv"
            csv_path.write_text(
                "symbol,name,list_date,delist_date,is_active,updated_at\n"
                "000001,One,2020-01-01,,true,2024-01-01\n"
                "000001,One New,bad-date,,true,2025-01-01\n"
                "000002,Two,2021-02-01,2024-01-01,false,2024-02-01\n",
                encoding="utf-8",
            )
            frame, warnings = load_historical_listings(csv_path)
            self.assertEqual(set(frame["symbol"]), {"000001", "000002"})
            self.assertTrue(any("invalid_date_count:list_date:1" in item for item in warnings))
            self.assertTrue(any("duplicate_symbol_count" in item for item in warnings))

    def test_load_historical_listings_skips_bad_rows_with_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "historical_listings.csv"
            csv_path.write_text(
                "symbol,name,list_date,delist_date,is_active,updated_at\n"
                "000001,One,2020-01-01,,true,2024-01-01\n"
                "bad,row,with,too,many,columns,for,this,pit,file\n",
                encoding="utf-8-sig",
            )

            frame, warnings = load_historical_listings(csv_path)

        self.assertEqual(list(frame["symbol"]), ["000001"])
        self.assertTrue(any("csv_bad_lines_skipped" in item for item in warnings))

    def test_universe_at_filters_listing_delist_st_and_suspend(self) -> None:
        listings = pd.DataFrame(
            {
                "symbol": ["000001", "000002", "000003", "000004"],
                "list_date": pd.to_datetime(["2020-01-01", "2025-01-01", "2020-01-01", "2020-01-01"]),
                "delist_date": [pd.NaT, pd.NaT, pd.Timestamp("2023-01-01"), pd.NaT],
                "st_start_date": [pd.NaT, pd.NaT, pd.NaT, pd.Timestamp("2024-01-01")],
                "st_end_date": [pd.NaT, pd.NaT, pd.NaT, pd.Timestamp("2024-12-31")],
                "suspend_start_date": [pd.NaT, pd.NaT, pd.NaT, pd.NaT],
                "suspend_end_date": [pd.NaT, pd.NaT, pd.NaT, pd.NaT],
                "is_active_bool": [True, True, False, True],
            }
        )
        symbols, meta = universe_at("2024-06-01", listings, include_st=False)
        self.assertEqual(symbols, ["000001"])
        self.assertEqual(meta["excluded_counts"]["not_listed_yet"], 1)
        self.assertEqual(meta["excluded_counts"]["delisted"], 1)
        self.assertEqual(meta["excluded_counts"]["st"], 1)

        listings.loc[0, "suspend_start_date"] = pd.Timestamp("2024-05-01")
        listings.loc[0, "suspend_end_date"] = pd.Timestamp("2024-07-01")
        symbols, meta = universe_at("2024-06-01", listings, include_suspended=False)
        self.assertEqual(symbols, [])
        self.assertEqual(meta["excluded_counts"]["suspended"], 1)

    def test_build_panel_and_status_are_json_safe(self) -> None:
        listings = pd.DataFrame(
            {
                "symbol": ["000001", "000002"],
                "list_date": pd.to_datetime(["2020-01-01", "2024-01-01"]),
                "delist_date": [pd.NaT, pd.NaT],
            }
        )
        panel = build_pit_universe_panel(["2023-12-31", "2024-01-02"], listings)
        self.assertEqual(set(panel.columns), {"date", "symbol", "is_in_universe"})
        self.assertFalse(panel.isna().any().any())
        status = summarize_pit_universe_status(listings)
        self.assertTrue(status["available"])
        self.assertTrue(status["pit_universe"])
        for key in ("list_date_coverage", "delist_date_coverage", "st_coverage", "suspend_coverage"):
            self.assertGreaterEqual(status[key], 0.0)
            self.assertLessEqual(status[key], 1.0)
        self.assertFalse(_has_bad_number(status))

    def test_degraded_price_coverage_uses_local_metadata_for_pit_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = root / "metadata"
            metadata.mkdir(parents=True)
            (metadata / "st_history.csv").write_text(
                "symbol,name,st_start_date,st_end_date,st_type\n"
                "000001,One,2021-01-01,2021-06-30,ST\n",
                encoding="utf-8",
            )
            (metadata / "delist_history.csv").write_text(
                "symbol,name,delist_date,last_trade_date,delist_reason\n"
                "000002,Two,2022-12-31,2022-12-30,merged\n",
                encoding="utf-8",
            )
            (metadata / "suspension_status.csv").write_text(
                "trade_date,symbol,is_suspended,suspension_reason\n"
                "2021-03-01,000001,True,S\n",
                encoding="utf-8",
            )
            listings = pd.DataFrame(
                {
                    "symbol": ["000001", "000002", "000003"],
                    "list_date": pd.to_datetime(["2020-01-01", "2020-01-01", "2020-01-01"]),
                    "delist_date": [pd.NaT, pd.NaT, pd.NaT],
                    "source": ["price_csv_coverage", "price_csv_coverage", "price_csv_coverage"],
                    "confidence": ["low_to_medium", "low_to_medium", "low_to_medium"],
                }
            )

            status = summarize_pit_universe_status(listings, metadata_dir=metadata)

        self.assertEqual(status["source"], "price_csv_coverage+local_metadata")
        self.assertEqual(status["pit_quality"], "partial_metadata_enriched")
        self.assertGreater(status["st_coverage"], 0.0)
        self.assertGreater(status["delist_date_coverage"], 0.0)
        self.assertGreater(status["suspend_coverage"], 0.0)
        self.assertNotIn("historical_st_periods_unavailable", status["warnings"])
        self.assertIn("degraded_pit_price_csv_coverage", status["warnings"])
        self.assertEqual(status["metadata_coverage"]["st_symbols"], 1)
        self.assertEqual(status["metadata_coverage"]["delist_symbols"], 1)
        self.assertEqual(status["metadata_coverage"]["suspension_symbols"], 1)

    def test_metadata_coverage_skips_bad_rows_with_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = root / "metadata"
            metadata.mkdir(parents=True)
            (metadata / "st_history.csv").write_text(
                "symbol,name,st_start_date,st_end_date,st_type\n"
                "000001,One,2021-01-01,2021-06-30,ST\n"
                "bad,row,with,too,many,columns,for,this,metadata,file\n",
                encoding="utf-8-sig",
            )
            listings = pd.DataFrame(
                {
                    "symbol": ["000001"],
                    "list_date": pd.to_datetime(["2020-01-01"]),
                    "delist_date": [pd.NaT],
                    "source": ["price_csv_coverage"],
                    "confidence": ["low_to_medium"],
                }
            )

            status = summarize_pit_universe_status(listings, metadata_dir=metadata)

        self.assertEqual(status["metadata_coverage"]["st_symbols"], 1)
        self.assertTrue(any("csv_bad_lines_skipped" in item for item in status["warnings"]))

    def test_cli_and_qd_json_summary(self) -> None:
        commands = [
            [sys.executable, "scripts/show_pit_universe_status.py", "--json", "true", "--summary", "true"],
            [sys.executable, "scripts/qd.py", "pit-universe", "--json", "true", "--summary", "true"],
        ]
        for command in commands:
            completed = subprocess.run(command, cwd=PROJECT_ROOT, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["task_type"], "point_in_time_universe")
            self.assertIn("warning_count", payload)
            self.assertIn("output_paths", payload)
        help_text = subprocess.check_output([sys.executable, "scripts/qd.py", "--help"], cwd=PROJECT_ROOT, text=True, encoding="utf-8", errors="replace")
        self.assertIn("pit-universe", help_text)

    def test_research_dashboard_pit_status_and_hygiene_docs(self) -> None:
        missing_status = summarize_pit_universe_status(pd.DataFrame())
        missing_payload = build_factor_research_dashboard(build_synthetic_research_payloads(), synthetic_payloads=True, pit_status=missing_status)
        self.assertFalse(missing_payload["pit_status"]["pit_universe"])
        self.assertIn(PIT_UNIVERSE_WARNING, missing_payload["dashboard_summary"]["risk_flags"])

        available_status = summarize_pit_universe_status(
            pd.DataFrame({"symbol": ["000001"], "list_date": pd.to_datetime(["2020-01-01"]), "delist_date": [pd.NaT]})
        )
        available_payload = build_factor_research_dashboard(build_synthetic_research_payloads(), synthetic_payloads=True, pit_status=available_status)
        self.assertTrue(available_payload["pit_status"]["pit_universe"])
        self.assertNotIn(PIT_UNIVERSE_WARNING, available_payload["dashboard_summary"]["risk_flags"])
        self.assertNotIn("reconstruction is not enabled", "\n".join(available_payload["assumptions"]))

        self.assertIn("test_runtime/", (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8"))
        self.assertFalse((PROJECT_ROOT / "architecture_inventory.md").exists())
        self.assertIn(
            "Updated through `v3.4.3.8-true-backtest-assumptions-split-and-doc-hygiene`",
            ARCHITECTURE_INVENTORY.read_text(encoding="utf-8"),
        )
        self.assertLess(len((PROJECT_ROOT / "README.md").read_text(encoding="utf-8").splitlines()), 550)
        self.assertTrue((PROJECT_ROOT / "docs" / "faq.md").exists())

    def test_factor_combiner_uses_available_pit_status_without_not_enabled_warning(self) -> None:
        panel = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2026-01-01", "2026-01-01", "2026-01-02", "2026-01-02"]
                ),
                "symbol": ["000001", "000002", "000001", "000002"],
                "momentum_20d": [0.1, 0.2, 0.3, 0.4],
                "market_cap": [10.0, 20.0, 11.0, 21.0],
            }
        )
        pit_status = {
            "available": True,
            "pit_universe": True,
            "pit_quality": "partial_metadata_enriched",
            "warnings": ["degraded_pit_price_csv_coverage"],
        }

        payload = build_combined_score(
            panel,
            ["momentum_20d"],
            neutralize_industry=False,
            neutralize_market_cap=False,
            winsorize=False,
            pit_universe_status=pit_status,
        )
        report = build_combined_score_report(payload)

        self.assertTrue(payload["universe"]["pit_universe"])
        self.assertIn("degraded_pit_price_csv_coverage", payload["warnings"])
        self.assertNotIn(PIT_UNIVERSE_WARNING, payload["warnings"])
        self.assertNotIn("reconstruction is not enabled", "\n".join(payload["assumptions"]))
        self.assertNotIn("reconstruction is not enabled", report)


if __name__ == "__main__":
    unittest.main()

