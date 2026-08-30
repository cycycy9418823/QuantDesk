from __future__ import annotations

import ast
import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class TradingCalendarStaticDataTests(unittest.TestCase):
    def test_calendar_csv_exists_and_covers_required_range(self) -> None:
        path = ROOT / "data" / "calendar" / "a_share_trading_calendar.csv"
        self.assertTrue(path.exists(), "data/calendar/a_share_trading_calendar.csv must exist")
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertGreater(len(rows), 0)
        dates = [row["date"] for row in rows]
        self.assertLessEqual(min(dates), "2020-01-01")
        self.assertGreaterEqual(max(dates), "2027-12-31")

    def test_2027_rows_are_marked_provisional(self) -> None:
        path = ROOT / "data" / "calendar" / "a_share_trading_calendar.csv"
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [row for row in csv.DictReader(handle) if row["date"].startswith("2027-")]
        self.assertGreater(len(rows), 0)
        combined = "\n".join((row.get("source", "") + " " + row.get("note", "")).lower() for row in rows)
        self.assertIn("provisional", combined)

    def test_calendar_info_uses_local_calendar_not_weekend_only(self) -> None:
        from quantdesk.trading_calendar import calendar_info

        info = calendar_info(start_date="2020-01-01", end_date="2027-12-31")
        self.assertFalse(info["weekend_only"])
        self.assertIn(info["calendar_quality"], {"local_calendar", "local_calendar_provisional"})
        self.assertGreater(info["loaded_dates"], 0)
        self.assertLessEqual(info["start_date"], "2020-01-01")
        self.assertGreaterEqual(info["end_date"], "2027-12-31")

    def test_weekend_and_exchange_holiday_are_closed(self) -> None:
        from quantdesk.trading_calendar import is_trade_day

        self.assertFalse(is_trade_day("2026-05-16"), "ordinary Saturday must not be a trade day")
        self.assertFalse(is_trade_day("2024-10-01"), "National Day holiday is a non-weekend market closure")

    def test_self_check_calendar_passes_when_static_csv_exists(self) -> None:
        from scripts.self_check import check_local_a_share_calendar

        result = check_local_a_share_calendar()
        self.assertEqual(result.status, "PASS", result.detail)

    def test_build_trading_calendar_offline_writes_csv_without_network(self) -> None:
        from scripts.build_trading_calendar import _parse_args, run

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "calendar.csv"
            args = _parse_args([
                "--start-date",
                "2024-10-01",
                "--end-date",
                "2024-10-08",
                "--output",
                str(output),
                "--network",
                "false",
                "--source",
                "offline",
            ])
            payload = run(args)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["network"])
            self.assertTrue(output.exists())
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["date"], "2024-10-01")
            self.assertEqual(rows[0]["is_open"], "false")


class VersionMetadataTests(unittest.TestCase):
    def test_pyproject_uses_dynamic_version_and_not_static_300(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn('version = "3.0.0"', text)
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn('[tool.setuptools.dynamic]', text)
        self.assertIn('version = {attr = "quantdesk.version.PACKAGE_VERSION"}', text)

    def test_package_version_matches_app_version_numeric_prefix(self) -> None:
        import quantdesk.version as version_info

        self.assertTrue(hasattr(version_info, "PACKAGE_VERSION"))
        app_numeric_prefix = version_info.APP_VERSION.split("-", 1)[0]
        self.assertEqual(version_info.PACKAGE_VERSION, app_numeric_prefix)
        self.assertEqual(version_info.API_SCHEMA_VERSION, app_numeric_prefix)


class MootdxProviderCleanupTests(unittest.TestCase):
    def _function_count(self, name: str) -> int:
        source = (ROOT / "quantdesk" / "providers" / "mootdx_provider.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        return sum(1 for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)

    def test_mootdx_provider_fetch_functions_defined_once(self) -> None:
        for name in ("fetch_realtime_quote", "fetch_daily_bars", "fetch_intraday_bars", "fetch_fundamentals"):
            self.assertEqual(self._function_count(name), 1, f"{name} should be defined once")

    def test_mootdx_network_false_returns_skipped_without_client_import(self) -> None:
        from quantdesk.providers import mootdx_provider

        with mock.patch.object(mootdx_provider, "_import_quotes_client", side_effect=AssertionError("network call")):
            quote = mootdx_provider.fetch_realtime_quote("600744", network=False)
            daily = mootdx_provider.fetch_daily_bars("600744", network=False)
            intraday = mootdx_provider.fetch_intraday_bars("600744", target_date="2026-05-19", network=False)
            fundamentals = mootdx_provider.fetch_fundamentals("600744", network=False)
        for payload in (quote, daily, intraday, fundamentals):
            self.assertEqual(payload.get("status"), "skipped")
            self.assertFalse(payload.get("ok", False))
            self.assertIn("skipped_reason", payload)


class BacktestAssumptionAndDocsTests(unittest.TestCase):
    def test_default_backtest_assumptions_include_board_candidate_execution_note(self) -> None:
        from quantdesk.backtest import default_backtest_assumptions

        assumptions = default_backtest_assumptions()
        self.assertIn("board_candidate_execution", assumptions)
        self.assertIn("不代表真实可成交", assumptions["board_candidate_execution"])

    def test_gitignore_contains_quantdesk_test_tmp(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".quantdesk_test_tmp/", text)

    def test_known_logic_issues_refreshed_for_v322(self) -> None:
        text = (ROOT / "docs" / "known_logic_issues.md").read_text(encoding="utf-8", errors="ignore")
        first_section = text[:1600]
        self.assertIn("v3.2.2", first_section)
        self.assertIn("Current", first_section)
        self.assertIn("Fixed", first_section)
        self.assertIn("Watchlist", first_section)
        self.assertNotIn("> v2.", first_section)

    def test_architecture_inventory_contains_current_key_modules(self) -> None:
        text = (ROOT / "docs" / "architecture_inventory.md").read_text(encoding="utf-8", errors="ignore")
        required = [
            "v3.2.2",
            "providers/*",
            "provider_result_contract.py",
            "provider_network_smoke.py",
            "trading_calendar.py",
            "intraday_*",
            "chanlun_*",
            "strategy_observation_contract.py",
            "agent_policy_guard.py",
            "agent_safe_bridge.py",
            "agent_evidence_pack.py",
            "agent_hallucination_guard.py",
            "agent_report_audit.py",
            "llm_config.py",
            "llm_providers/*",
            "not constitute investment advice",
        ]
        missing = [token for token in required if token not in text]
        self.assertFalse(missing, "architecture inventory missing: " + ", ".join(missing))

    def test_export_rules_keep_calendar_and_exclude_runtime_outputs(self) -> None:
        text = (ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
        self.assertIn('"results"', text)
        self.assertIn('".quantdesk_test_tmp"', text)
        self.assertIn('"data\\calendar"', text)
        self.assertIn('"data\\daily"', text)
        self.assertIn('"data\\intraday"', text)


if __name__ == "__main__":
    unittest.main()


