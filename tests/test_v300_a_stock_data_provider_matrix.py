from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AStockDataProviderMatrixV300Tests(unittest.TestCase):
    def test_provider_matrix_contains_core_providers(self) -> None:
        from quantdesk.a_stock_data_matrix import build_provider_matrix_payload

        payload = build_provider_matrix_payload(network=False)
        providers = payload["providers"]
        names = {row["provider"] for row in providers}
        self.assertIn("mootdx", names)
        self.assertIn("tencent", names)
        self.assertIn("akshare", names)

    def test_tushare_and_ashare_are_disabled(self) -> None:
        from quantdesk.a_stock_data_matrix import build_provider_matrix_payload

        rows = build_provider_matrix_payload(network=False)["providers"]
        tushare = [row for row in rows if row["provider"] == "tushare"]
        ashare = [row for row in rows if row["provider"] == "ashare"]
        self.assertTrue(tushare)
        self.assertTrue(ashare)
        self.assertTrue(all(row["status"] == "deprecated_or_disabled" for row in tushare))
        self.assertTrue(all(row["status"] == "deprecated_or_disabled" for row in ashare))

    def test_iwencai_default_planned_placeholder(self) -> None:
        from quantdesk.a_stock_data_matrix import build_provider_matrix_payload

        rows = build_provider_matrix_payload(network=False)["providers"]
        iwencai = [row for row in rows if row["provider"] == "iwencai"]
        self.assertTrue(iwencai)
        self.assertEqual(iwencai[0]["status"], "planned")
        self.assertTrue(iwencai[0]["placeholder"])
        self.assertTrue(iwencai[0]["requires_api_key"])

    def test_registry_by_category(self) -> None:
        from quantdesk.a_stock_data_matrix import providers_by_category
        from quantdesk.data_provider_registry import a_stock_data_provider_matrix_payload

        quote = providers_by_category("market_quote")
        self.assertEqual([row["provider"] for row in quote[:2]], ["mootdx", "tencent"])
        payload = a_stock_data_provider_matrix_payload(categories=["market_quote"], network=False)
        self.assertTrue(payload["providers"])

    def test_daily_and_intraday_provider_order(self) -> None:
        from quantdesk.a_stock_data_matrix import providers_by_category

        daily = {row["provider"] for row in providers_by_category("daily_kline")}
        intraday = {row["provider"] for row in providers_by_category("intraday_kline")}
        self.assertTrue({"local_existing", "mootdx", "tencent", "akshare"}.issubset(daily))
        self.assertTrue({"local_existing", "mootdx", "tencent", "akshare"}.issubset(intraday))

    def test_capability_fields_are_present(self) -> None:
        from quantdesk.a_stock_data_matrix import build_provider_matrix_payload

        row = build_provider_matrix_payload(network=False)["providers"][0]
        for key in ("supports_current_day", "supports_historical_day", "suggested_next_action", "fallback_next_action"):
            self.assertIn(key, row)

    def test_cli_default_network_false(self) -> None:
        script = PROJECT_ROOT / "scripts" / "check_a_stock_data_providers.py"
        text = script.read_text(encoding="utf-8")
        self.assertIn('default="false"', text)
        self.assertIn("network=false", text)
        cmd = [sys.executable, str(script), "--categories", "quote", "--json"]
        result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"network": false', result.stdout)

    def test_adapter_failure_returns_envelope(self) -> None:
        from unittest import mock

        import pandas as pd

        from quantdesk import a_stock_data_adapter

        with mock.patch.object(a_stock_data_adapter, "read_local_daily_with_warnings", return_value=(pd.DataFrame(), [])):
            payload = a_stock_data_adapter.get_daily_bars("600744", "2026-05-01", "2026-05-15", network=False)
        self.assertFalse(payload["ok"])
        self.assertIn("provider_chain", payload)
        self.assertTrue(payload["provider_chain"])

    def test_adapter_invalid_date_filter_does_not_return_unfiltered_local_rows(self) -> None:
        from unittest import mock

        import pandas as pd

        from quantdesk import a_stock_data_adapter

        local = pd.DataFrame([{"date": "2026-05-15", "close": 10.0}])
        with mock.patch.object(a_stock_data_adapter, "read_local_daily_with_warnings", return_value=(local, [])):
            payload = a_stock_data_adapter.get_daily_bars("600744", "not-a-date", "2026-05-15", network=False)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["data"], [])
        self.assertIn("invalid_daily_date_filter", payload["warnings"])
        self.assertIn("invalid_start_date:not-a-date", payload["errors"])
        self.assertEqual(payload["provider_chain"][0]["provider"], "request")

    def test_adapter_surfaces_local_daily_cache_read_failure(self) -> None:
        from unittest import mock

        from quantdesk import a_stock_data_adapter

        tmp_root = PROJECT_ROOT / ".tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
            daily_dir = Path(tmp)
            (daily_dir / "600744.csv").write_text("", encoding="utf-8-sig")
            with mock.patch.object(a_stock_data_adapter, "DAILY_DIR", daily_dir):
                payload = a_stock_data_adapter.get_daily_bars("600744", "2026-05-15", "2026-05-15", network=False)
        self.assertFalse(payload["ok"])
        self.assertTrue(any("local_daily_cache_read_failed:600744" in item for item in payload["warnings"]))
        self.assertEqual(payload["provider_chain"][0]["provider"], "local_existing")
        self.assertEqual(payload["provider_chain"][0]["status"], "failed")

    def test_data_health_dashboard_includes_data_source_trace(self) -> None:
        from quantdesk.data_health_dashboard import build_data_health_dashboard

        payload = build_data_health_dashboard(
            daily_price_payload={"ok": True, "total_symbols": 1, "stale_count": 0, "missing_count": 0, "target_date": "2026-05-15", "latest_local_date": "2026-05-15"},
            freshness_payload={"target_complete_trade_date": "2026-05-15", "local_daily_latest_date": "2026-05-15", "is_daily_stale": False},
            intraday_payload={"total_symbols": 1, "available_count": 0, "missing_count": 1, "target_trade_date": "2026-05-15"},
            theme_payload={"total": 1, "mapped_count": 1, "unknown_count": 0},
            current_candidate_theme_payload={"candidate_count": 0},
            provider_capability_payload={"summary": {"ok_count": 0}},
            portfolio_payload={"exists": True, "positions": []},
            daily_workflow_payload={},
            real_sample_payload={},
            self_validation_payload={},
            strategy_payload={"long_candidates": []},
            theme_map={},
            write=False,
        )
        self.assertIn("data_source_trace", payload)
        self.assertIn("mootdx_daily", payload["data_source_trace"]["daily_data"])


    def test_provider_setup_docs_and_safety_text(self) -> None:
        docs = (PROJECT_ROOT / "docs" / "provider_setup.md").read_text(encoding="utf-8")
        for token in ("akshare", "mootdx", "iwencai", "不接券商", "不真实下单", "不构成投资建议"):
            self.assertIn(token, docs)


if __name__ == "__main__":
    unittest.main()


