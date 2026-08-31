from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RealTencentProviderAndEntrypointTests(unittest.TestCase):
    def test_to_tencent_symbol_sh_and_sz(self) -> None:
        from quantdesk.providers.tencent_provider import to_tencent_symbol

        self.assertEqual(to_tencent_symbol("600744")["tencent_symbol"], "sh600744")
        self.assertEqual(to_tencent_symbol("002115")["tencent_symbol"], "sz002115")
        self.assertEqual(to_tencent_symbol("002115")["normalized_symbol"], "002115")
        self.assertEqual(to_tencent_symbol("600744.SH")["tencent_symbol"], "sh600744")
        self.assertEqual(to_tencent_symbol("sz002115")["tencent_symbol"], "sz002115")

    def test_network_false_skips_without_http(self) -> None:
        from quantdesk.providers import tencent_provider

        with mock.patch.object(tencent_provider, "_http_get_bytes", side_effect=AssertionError("network called")):
            quote = tencent_provider.fetch_realtime_quote(["600744"], network=False)
            daily = tencent_provider.fetch_daily_bars("600744", network=False)
        self.assertEqual(quote["status"], "skipped")
        self.assertEqual(daily["status"], "skipped")
        self.assertIn("network=false", quote["skipped_reason"])

    def test_mock_realtime_quote_parses_core_fields(self) -> None:
        from quantdesk.providers import tencent_provider

        fields = [""] * 60
        fields[0] = "1"
        fields[1] = "华银电力"
        fields[2] = "600744"
        fields[3] = "8.45"
        fields[4] = "8.30"
        fields[5] = "8.32"
        fields[6] = "12345"
        fields[30] = "20260515102300"
        fields[33] = "8.60"
        fields[34] = "8.20"
        fields[36] = "12345"
        fields[37] = "9876.50"
        fields[38] = "2.50"
        fields[39] = "18.20"
        fields[43] = "4.80"
        fields[44] = "120.00"
        fields[45] = "180.00"
        text = f'v_sh600744="{"~".join(fields)}";'.encode("gbk")
        with mock.patch.object(tencent_provider, "_http_get_bytes", return_value=text):
            payload = tencent_provider.fetch_realtime_quote(["600744"], network=True)
        self.assertTrue(payload["ok"], payload)
        row = payload["data"][0]
        self.assertEqual(row["symbol"], "600744")
        self.assertEqual(row["price"], 8.45)
        self.assertEqual(row["open"], 8.32)
        self.assertEqual(row["high"], 8.60)
        self.assertEqual(row["low"], 8.20)
        self.assertEqual(row["volume"], 12345)

    def test_mock_daily_bars_parse_and_filter(self) -> None:
        from quantdesk.providers import tencent_provider

        payload = {
            "data": {
                "sh600744": {
                    "qfqday": [
                        ["2026-05-13", "8.00", "8.20", "8.30", "7.90", "1000", "8200"],
                        ["2026-05-14", "8.20", "8.40", "8.50", "8.10", "1100", "9100"],
                        ["2026-05-15", "8.40", "8.45", "8.60", "8.30", "1200", "10000"],
                    ]
                }
            }
        }
        with mock.patch.object(tencent_provider, "_http_get_bytes", return_value=str.encode(__import__("json").dumps(payload), "utf-8")):
            result = tencent_provider.fetch_daily_bars("600744", "2026-05-14", "2026-05-15", network=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual([row["date"] for row in result["data"]], ["2026-05-14", "2026-05-15"])
        self.assertEqual(result["data"][0]["open"], 8.20)
        self.assertEqual(result["data"][1]["close"], 8.45)

    def test_daily_qfq_fallback_to_day_reports_effective_raw_basis(self) -> None:
        from quantdesk.providers import tencent_provider

        payload = {
            "data": {
                "bj920000": {
                    "day": [
                        ["2026-07-08", "11.49", "11.22", "11.69", "11.16", "3273"],
                    ]
                }
            }
        }
        encoded = __import__("json").dumps(payload).encode("utf-8")
        with mock.patch.object(tencent_provider, "_http_get_bytes", return_value=encoded):
            result = tencent_provider.fetch_daily_bars(
                "920000",
                "2026-07-08",
                "2026-07-08",
                network=True,
                adjust="qfq",
            )

        self.assertTrue(result["ok"], result)
        self.assertIn("Tencent did not return qfqday; fallback to day data.", result["warnings"])
        self.assertEqual(result["requested_adjust"], "qfq")
        self.assertEqual(result["effective_adjust"], "raw")
        self.assertEqual(result["adjust"], "raw")
        self.assertEqual(result["data"][0]["adjust"], "raw")

    def test_missing_realtime_fields_warn_but_do_not_crash(self) -> None:
        from quantdesk.providers import tencent_provider

        text = b'v_sh600744="1~name~600744~8.45";'
        with mock.patch.object(tencent_provider, "_http_get_bytes", return_value=text):
            payload = tencent_provider.fetch_realtime_quote(["600744"], network=True)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["warnings"])

    def test_request_failure_has_failed_reason(self) -> None:
        from quantdesk.providers import tencent_provider

        with mock.patch.object(tencent_provider, "_http_get_bytes", side_effect=RuntimeError("network down")):
            payload = tencent_provider.fetch_realtime_quote(["600744"], network=True)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("network down", payload["failed_reason"])

    def test_adapter_uses_tencent_daily_and_provider_chain(self) -> None:
        from quantdesk import a_stock_data_adapter

        with mock.patch.object(a_stock_data_adapter, "read_local_daily_with_warnings", return_value=(__import__("pandas").DataFrame(), [])):
            with mock.patch(
                "quantdesk.providers.mootdx_provider.fetch_daily_bars",
                return_value={"ok": False, "status": "failed", "failed_reason": "fixture fallback"},
            ):
                with mock.patch("quantdesk.providers.tencent_provider.fetch_daily_bars", return_value={
                    "ok": True,
                    "status": "ok",
                    "data": [{"symbol": "600744", "date": "2026-05-15", "open": 8.4, "high": 8.6, "low": 8.3, "close": 8.45, "volume": 1200, "amount": 10000}],
                    "warnings": [],
                    "errors": [],
                }):
                    payload = a_stock_data_adapter.get_daily_bars("600744", "2026-05-15", "2026-05-15", network=True)
        self.assertTrue(payload["ok"])
        self.assertIn("tencent_daily", [row["provider"] for row in payload["provider_chain"]])

    def test_daily_price_update_default_providers_include_tencent(self) -> None:
        from quantdesk.daily_price_update_engine import DEFAULT_PROVIDERS

        self.assertIn("tencent_daily", DEFAULT_PROVIDERS)
        self.assertLess(DEFAULT_PROVIDERS.index("tencent_daily"), DEFAULT_PROVIDERS.index("akshare_daily"))

    def test_intraday_update_historical_tencent_skipped_reason(self) -> None:
        from quantdesk.intraday.intraday_update_engine import update_intraday_1m_for_symbols

        with tempfile.TemporaryDirectory() as tmp:
            payload = update_intraday_1m_for_symbols(
                symbols=["600744"],
                target_date="2026-05-15",
                provider_order=["tencent_intraday"],
                source="symbols",
                network=True,
                dry_run=False,
                intraday_dir=Path(tmp),
                output_path=Path(tmp) / "intraday_update.json",
            )
        row = payload["provider_results"][0]
        self.assertEqual(row["provider"], "tencent_intraday")
        self.assertIn("历史目标日期", row.get("skipped_reason", ""))

    def test_qd_help_and_mapping(self) -> None:
        qd = PROJECT_ROOT / "scripts" / "qd.py"
        result = subprocess.run([sys.executable, str(qd), "help"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        for token in ("self-check", "providers", "daily", "chanlun-segment"):
            self.assertIn(token, result.stdout)
        text = qd.read_text(encoding="utf-8")
        self.assertIn("self_check.py", text)

    def test_readme_and_active_desk_safety_text(self) -> None:
        """The three boundary promises must be visible wherever a user reads.

        Re-anchored from the retired app.js / index.html onto the active template
        and bundle, which carry all three phrases. The old 默认未联网检测 wording
        exists only in the README on this tree, so it is asserted there rather than
        against a desk that never uses that exact phrase.
        """
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        html = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(
            encoding="utf-8"
        )
        bundle = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("新手只需要记住 5 个命令", readme)
        self.assertIn("默认未联网检测", readme)
        desk = html + bundle
        for phrase in ("不接券商", "不真实下单", "不构成投资建议"):
            self.assertIn(phrase, desk, f"active desk no longer states {phrase}")


if __name__ == "__main__":
    unittest.main()


