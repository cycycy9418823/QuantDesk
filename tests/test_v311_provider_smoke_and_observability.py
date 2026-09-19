from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _quote_bytes() -> bytes:
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
    fields[46] = "1.50"
    return f'v_sh600744="{"~".join(fields)}";'.encode("gbk")


def _daily_bytes() -> bytes:
    payload = {
        "data": {
            "sh600744": {
                "qfqday": [
                    ["2026-05-14", "8.20", "8.40", "8.50", "8.10", "1100", "9100"],
                    ["2026-05-15", "8.40", "8.45", "8.60", "8.30", "1200", "10000"],
                ]
            }
        }
    }
    return json.dumps(payload).encode("utf-8")


def _mock_tencent_http(url: str, timeout: int = 8) -> bytes:
    if "qt.gtimg.cn" in url:
        return _quote_bytes()
    if "fqkline/get" in url:
        return _daily_bytes()
    raise AssertionError(f"unexpected network call: {url}")


class ProviderSmokeObservabilityTests(unittest.TestCase):
    def test_network_false_smoke_does_not_call_http(self) -> None:
        from quantdesk.provider_network_smoke import run_provider_network_smoke
        from quantdesk.providers import tencent_provider

        with mock.patch.object(tencent_provider, "_http_get_bytes", side_effect=AssertionError("network called")):
            payload = run_provider_network_smoke(symbols="600744", target_date="2026-05-15", network=False, write=False)
        self.assertEqual(payload["summary"]["skipped"], 4)
        self.assertEqual(payload["summary"]["failed"], 0)
        self.assertTrue(all(step["status"] == "skipped" for step in payload["steps"]))

    def test_low_price_sample_skips_bad_rows(self) -> None:
        from quantdesk.provider_network_smoke import load_low_price_sample

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "low_price_learning_symbols.csv"
            path.write_text(
                "\n".join(
                    [
                        "symbol,included",
                        "002115,True",
                        "bad,row,with,too,many,columns",
                        "600000,True",
                    ]
                ),
                encoding="utf-8-sig",
            )

            symbols = load_low_price_sample(limit=3, universe_path=path)

        self.assertEqual(symbols, ["002115", "600000"])

    def test_provider_smoke_mock_quote_and_daily_success(self) -> None:
        from quantdesk.provider_network_smoke import run_provider_network_smoke
        from quantdesk.providers import tencent_provider

        with mock.patch.object(tencent_provider, "_http_get_bytes", side_effect=_mock_tencent_http):
            payload = run_provider_network_smoke(symbols="600744", target_date="2026-05-15", network=True, write=False)
        by_label = {step["label"]: step for step in payload["steps"]}
        self.assertEqual(by_label["realtime quote"]["status"], "ok")
        self.assertEqual(by_label["realtime quote"]["data_rows"], 1)
        self.assertEqual(by_label["daily bars"]["status"], "ok")
        self.assertEqual(by_label["daily bars"]["data_rows"], 1)
        self.assertEqual(by_label["valuation"]["status"], "ok")

    def test_historical_intraday_is_skipped_not_faked(self) -> None:
        from quantdesk.provider_network_smoke import run_provider_network_smoke
        from quantdesk.providers import tencent_provider

        with mock.patch.object(tencent_provider, "_http_get_bytes", side_effect=_mock_tencent_http):
            payload = run_provider_network_smoke(symbols="600744", target_date="2026-05-15", network=True, write=False)
        intraday = [step for step in payload["steps"] if step["label"] == "intraday bars"][0]
        self.assertEqual(intraday["status"], "skipped")
        self.assertIn("腾讯财经当前适配仅支持当日分钟线", intraday["skipped_reason"])
        self.assertEqual(intraday["data_rows"], 0)

    def test_provider_result_contract_has_required_fields(self) -> None:
        from quantdesk.provider_result_contract import build_provider_result

        payload = build_provider_result(
            provider="tencent_daily",
            category="daily_kline",
            ok=False,
            status="failed",
            symbol="600744",
            failed_reason="network down",
            network=True,
            duration_ms=12,
        )
        for key in (
            "ok",
            "status",
            "provider",
            "provider_used",
            "category",
            "symbol",
            "data",
            "data_rows",
            "warnings",
            "errors",
            "failed_reason",
            "skipped_reason",
            "suggested_next_action",
            "provider_chain",
            "data_source_trace",
            "is_reliable",
            "network",
            "retrieved_at",
            "duration_ms",
            "disclaimer",
        ):
            self.assertIn(key, payload)

    def test_qd_network_smoke_command_is_wired(self) -> None:
        text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8")
        self.assertIn("network-smoke", text)
        self.assertIn("run_provider_network_smoke.py", text)

    def test_clean_zip_excludes_provider_network_smoke_result(self) -> None:
        text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("results", text)
        self.assertIn("reports", text)

    def test_tencent_timeout_or_exception_has_failed_reason(self) -> None:
        from quantdesk.providers import tencent_provider

        with mock.patch.object(tencent_provider, "_http_get_bytes", side_effect=TimeoutError("timed out")):
            payload = tencent_provider.fetch_realtime_quote(["600744"], network=True)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failed_type"], "timeout")
        self.assertIn("timed out", payload["failed_reason"])

    def test_missing_realtime_fields_warn_but_do_not_crash(self) -> None:
        from quantdesk.providers import tencent_provider

        with mock.patch.object(tencent_provider, "_http_get_bytes", return_value=b'v_sh600744="1~name~600744~8.45";'):
            payload = tencent_provider.fetch_realtime_quote(["600744"], network=True)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["warnings"])
        self.assertGreaterEqual(payload["duration_ms"], 0)


if __name__ == "__main__":
    unittest.main()


