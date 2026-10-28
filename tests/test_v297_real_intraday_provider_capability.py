from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from tests.minute_evidence_fixture import final_minute_fixture

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.intraday_providers import _normalize_provider_frame  # noqa: E402
from quantdesk.intraday.intraday_provider_capability import check_intraday_provider_capability  # noqa: E402
from quantdesk import real_intraday_sample_validation as sample_validation  # noqa: E402
from quantdesk.real_intraday_sample_validation import run_real_intraday_sample_validation  # noqa: E402
import scripts.check_intraday_provider_capability as capability_cli  # noqa: E402
import scripts.run_real_intraday_sample_validation as sample_cli  # noqa: E402


def _redirect_sample_validation_outputs(root: Path):
    """把样例验证的**落盘出口**（结果 JSON 与报告目录）重定向到测试临时目录。

    报告路径用的是模块级 ``REPORTS_DIR``（项目 ``reports/``）：不重定向就会写到源码树里，
    被 outside_write 围栏拦下（T08-ADJACENT-REPORT-ISOLATION-20260930）。这里只改目录，
    **不 mock 报告生成本身**，原断言不变。
    """

    return mock.patch.multiple(
        sample_validation,
        RESULTS_DIR=root / "results",
        REPORTS_DIR=root / "reports",
        CONDITIONAL_TRADE_PLANS_LATEST_JSON=root / "results" / "conditional_trade_plans_latest.json",
        REAL_SAMPLE_VALIDATION_LATEST_JSON=root / "results" / "real_intraday_sample_validation_latest.json",
        REAL_SAMPLE_EXECUTION_JSON=root / "results" / "real_intraday_sample_execution.json",
        REAL_SAMPLE_UPDATE_JSON=root / "results" / "real_intraday_sample_update_latest.json",
        AUTO_PAPER_PORTFOLIO_STATE_JSON=root / "state" / "auto_paper_portfolio.json",
    )


def _mock_frame(symbol: str = "600744", target_date: str = "2026-05-15") -> pd.DataFrame:
    # A full, valid 2-row OHLCV frame (real intrabar range, in-session times) so it
    # passes the unified quality gate. The 10:23:00 / close 8.45 row is preserved as
    # the trigger row the execution test asserts on.
    return pd.DataFrame([
        {
            "symbol": symbol,
            "datetime": f"{target_date} 10:23:00",
            "open": 8.44,
            "high": 8.46,
            "low": 8.43,
            "close": 8.45,
            "volume": 1000,
            "amount": 8450,
            # T08：合成分钟帧是未复权原始报价，显式声明口径。
            "adjust": "raw",
        },
        {
            "symbol": symbol,
            "datetime": f"{target_date} 10:24:00",
            "open": 8.45,
            "high": 8.47,
            "low": 8.44,
            "close": 8.46,
            "volume": 1100,
            "amount": 9306,
            "adjust": "raw",
        },
    ])


class RealIntradayProviderCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        # T08-ADJACENT-REPORT-ISOLATION-20260930：把样例验证的落盘出口改到独立临时目录。
        # 报告照旧真实生成，只是不再写到源码树的 reports/ 下（那会被 outside_write 围栏拦）。
        self._outputs_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._outputs_tmp.cleanup)
        patcher = _redirect_sample_validation_outputs(Path(self._outputs_tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_capability_defaults_to_offline_provider_calls(self) -> None:
        calls: list[bool] = []

        def fake_fetch(provider, symbol, target_date, **kwargs):
            calls.append(bool(kwargs.get("network")))
            return {"provider": provider, "status": "skipped", "symbol": symbol, "target_date": target_date, "skipped_reason": "network disabled", "data": pd.DataFrame()}

        with mock.patch("quantdesk.intraday.intraday_provider_capability.fetch_intraday_1m", side_effect=fake_fetch):
            payload = check_intraday_provider_capability(symbols="600744", dates="2026-05-15", provider_order=["akshare_intraday"], write=False)

        self.assertFalse(payload["network"])
        self.assertEqual(calls, [False])

    def test_capability_cli_requires_explicit_network(self) -> None:
        with mock.patch.object(capability_cli, "check_intraday_provider_capability", return_value={"ok": True}) as runner, mock.patch("builtins.print"):
            code = capability_cli.main(["--symbols", "600744", "--dates", "2026-05-15"])

        self.assertEqual(code, 0)
        self.assertFalse(runner.call_args.kwargs["network"])

        with mock.patch.object(capability_cli, "check_intraday_provider_capability", return_value={"ok": True}) as runner, mock.patch("builtins.print"):
            code = capability_cli.main(["--symbols", "600744", "--dates", "2026-05-15", "--network", "true"])

        self.assertEqual(code, 0)
        self.assertTrue(runner.call_args.kwargs["network"])

    def test_real_sample_validation_defaults_to_offline_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "quantdesk.real_intraday_sample_validation.update_intraday_1m_for_symbols",
            return_value={"ok": True, "updated_symbol_list": [], "unchanged_symbol_list": [], "failed_symbol_list": ["600744"]},
        ) as updater, mock.patch(
            "quantdesk.real_intraday_sample_validation.simulate_conditional_plan_file",
            return_value={"ok": True, "summary": {"event_count": 0, "missing_intraday_count": 1}, "events": []},
        ):
            root = Path(tmp)
            payload = run_real_intraday_sample_validation(
                symbols="600744",
                target_date="2026-05-15",
                provider_order=["akshare_intraday"],
                intraday_dir=root / "1m",
                output_path=root / "sample.json",
                execution_output_path=root / "execution.json",
                update_output_path=root / "update.json",
            )

        self.assertFalse(payload["network"])
        self.assertFalse(updater.call_args.kwargs["network"])

    def test_real_sample_cli_requires_explicit_network(self) -> None:
        with mock.patch.object(sample_cli, "run_real_intraday_sample_validation", return_value={"ok": True}) as runner, mock.patch("builtins.print"):
            code = sample_cli.main(["--symbols", "600744", "--target-date", "2026-05-15"])

        self.assertEqual(code, 0)
        self.assertFalse(runner.call_args.kwargs["network"])

        with mock.patch.object(sample_cli, "run_real_intraday_sample_validation", return_value={"ok": True}) as runner, mock.patch("builtins.print"):
            code = sample_cli.main(["--symbols", "600744", "--target-date", "2026-05-15", "--network", "true"])

        self.assertEqual(code, 0)
        self.assertTrue(runner.call_args.kwargs["network"])

    def test_provider_capability_output_fields_complete(self) -> None:
        def fake_fetch(provider, symbol, target_date, **_kwargs):
            return {
                "provider": provider,
                "status": "ok",
                "symbol": symbol,
                "target_date": target_date,
                "rows": 1,
                "latest_datetime": f"{target_date} 10:23:00",
                "data": _mock_frame(symbol, target_date),
                "failed_reason": "",
                "warnings": [],
            }

        with tempfile.TemporaryDirectory() as tmp, mock.patch("quantdesk.intraday.intraday_provider_capability.fetch_intraday_1m", side_effect=fake_fetch):
            payload = check_intraday_provider_capability(
                symbols="600744",
                dates="2026-05-15",
                provider_order=["akshare_intraday"],
                output_path=Path(tmp) / "capability.json",
            )
        row = payload["provider_results"][0]
        for key in ("provider", "dependency_installed", "network_request_success", "supports_current_day", "supports_historical_day", "supports_target_date", "date_scope", "rows", "latest_datetime", "failed_reason", "skipped_reason"):
            self.assertIn(key, row)
        self.assertTrue(row["supports_target_date"])

    def test_capability_preserves_non_formal_quality_and_safe_action(self) -> None:
        result = {
            "provider": "akshare_intraday",
            "status": "ok",
            "symbol": "600744",
            "target_date": "2026-05-15",
            "rows": 155,
            "data": _mock_frame("600744", "2026-05-15"),
            "ohlcv_complete": True,
            "signal_eligible": True,
            "price_only_intraday": False,
            "formal_execution_eligible": False,
            "formal_session_status": "partial_session",
            "suggested_next_action": "可执行条件交易计划。",
        }

        with mock.patch(
            "quantdesk.intraday.intraday_provider_capability.fetch_intraday_1m",
            return_value=result,
        ):
            payload = check_intraday_provider_capability(
                symbols="600744",
                dates="2026-05-15",
                provider_order=["akshare_intraday"],
                write=False,
            )

        row = payload["provider_results"][0]
        self.assertFalse(row["formal_execution_eligible"])
        self.assertEqual(row["formal_session_status"], "partial_session")
        self.assertNotIn("可执行条件交易计划", row["suggested_next_action"])
        self.assertIn("not formal", row["suggested_next_action"])

    def test_provider_failed_does_not_crash(self) -> None:
        with mock.patch("quantdesk.intraday.intraday_provider_capability.fetch_intraday_1m", return_value={"status": "failed", "failed_reason": "network down", "data": pd.DataFrame()}):
            payload = check_intraday_provider_capability(symbols="600744", dates="2026-05-15", provider_order=["akshare_intraday"], write=False)
        self.assertEqual(payload["summary"]["failed_count"], 1)
        self.assertIn("network down", payload["provider_results"][0]["failed_reason"])

    def test_provider_skipped_has_skipped_reason_and_current_day_scope(self) -> None:
        with mock.patch("quantdesk.intraday.intraday_provider_capability.fetch_intraday_1m", return_value={"status": "skipped", "failed_reason": "", "data": pd.DataFrame()}):
            payload = check_intraday_provider_capability(symbols="600744", dates="2026-05-15", provider_order=["tencent_intraday"], write=False)
        row = payload["provider_results"][0]
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["date_scope"], "current_day_only")
        self.assertIn("不支持历史目标日期", row["skipped_reason"])

    def test_sample_validation_no_data_does_not_write_fake_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intraday_dir = root / "1m"
            with mock.patch("quantdesk.intraday_update_engine.fetch_intraday_1m", return_value={"status": "failed", "failed_reason": "provider down", "data": pd.DataFrame()}):
                payload = run_real_intraday_sample_validation(
                    symbols="600744",
                    target_date="2026-05-15",
                    provider_order=["akshare_intraday"],
                    intraday_dir=intraday_dir,
                    output_path=root / "sample.json",
                    execution_output_path=root / "execution.json",
                    update_output_path=root / "update.json",
                )
            self.assertFalse((intraday_dir / "600744.csv").exists())
            self.assertEqual(payload["summary"]["updated_or_existing_count"], 0)
            self.assertGreaterEqual(payload["conditional_execution_summary"]["missing_intraday_count"], 1)

    def test_latest_daily_date_skips_bad_csv_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily_dir = Path(tmp)
            (daily_dir / "600744.csv").write_text(
                "\n".join(
                    [
                        "date,close",
                        "2026-05-14,8.4",
                        "bad,row,with,too,many,columns",
                        "2026-05-15,8.5",
                    ]
                ),
                encoding="utf-8-sig",
            )

            latest = sample_validation._latest_daily_date(["600744"], daily_dir=daily_dir)

            self.assertEqual(latest, "2026-05-15")

    def test_sample_validation_mock_minutes_write_csv_and_execute(self) -> None:
        def fake_fetch(provider, symbol, target_date, **_kwargs):
            # Route through the real quality gate so the result carries
            # ohlcv_complete / signal_eligible exactly like the real fetch path;
            # a raw dict would (correctly) fail-close as missing_quality_gate.
            return _normalize_provider_frame(
                provider, symbol, target_date,
                final_minute_fixture(_mock_frame(symbol, target_date).drop(columns=["adjust"])), source=provider,
            )

        with tempfile.TemporaryDirectory() as tmp, mock.patch("quantdesk.intraday_update_engine.fetch_intraday_1m", side_effect=fake_fetch):
            root = Path(tmp)
            intraday_dir = root / "1m"
            payload = run_real_intraday_sample_validation(
                symbols="600744",
                target_date="2026-05-15",
                provider_order=["akshare_intraday"],
                intraday_dir=intraday_dir,
                output_path=root / "sample.json",
                execution_output_path=root / "execution.json",
                update_output_path=root / "update.json",
            )
            self.assertTrue((intraday_dir / "600744.csv").exists())
        self.assertEqual(payload["summary"]["updated_or_existing_count"], 1)
        self.assertEqual(payload["conditional_execution_summary"]["missing_intraday_count"], 0)
        self.assertGreaterEqual(payload["conditional_execution_summary"]["event_count"], 1)
        event = payload["conditional_execution"]["events"][0]
        self.assertEqual(event["trigger_time"], "2026-05-15 10:23:00")
        self.assertEqual(float(event["trigger_price"]), 8.45)

    def test_update_portfolio_default_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch("quantdesk.intraday_update_engine.fetch_intraday_1m", return_value={"status": "failed", "failed_reason": "provider down", "data": pd.DataFrame()}):
            payload = run_real_intraday_sample_validation(
                symbols="600744",
                target_date="2026-05-15",
                provider_order=["akshare_intraday"],
                intraday_dir=Path(tmp) / "1m",
                output_path=Path(tmp) / "sample.json",
                execution_output_path=Path(tmp) / "execution.json",
                update_output_path=Path(tmp) / "update.json",
            )
        self.assertFalse(payload["update_portfolio"])
        self.assertTrue(payload["portfolio_update"]["skipped"])


    def test_report_contains_required_disclaimer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("quantdesk.intraday_update_engine.fetch_intraday_1m", return_value={"status": "failed", "failed_reason": "provider down", "data": pd.DataFrame()}):
                payload = run_real_intraday_sample_validation(
                    symbols="600744",
                    target_date="2026-05-15",
                    provider_order=["akshare_intraday"],
                    intraday_dir=root / "1m",
                    output_path=root / "sample.json",
                    execution_output_path=root / "execution.json",
                    update_output_path=root / "update.json",
                )
        report = PROJECT_ROOT / payload["artifacts"]["report_path"]
        text = report.read_text(encoding="utf-8")
        self.assertIn("真实 provider 数据验证", text)
        self.assertIn("不接券商", text)
        self.assertIn("不真实下单", text)
        self.assertIn("不构成投资建议", text)

    def test_report_humanizes_provider_and_minute_execution_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "quantdesk.real_intraday_sample_validation.update_intraday_1m_for_symbols",
                return_value={
                    "ok": True,
                    "updated_symbol_list": [],
                    "unchanged_symbol_list": [],
                    "failed_symbol_list": ["600744"],
                    "provider_results": [
                        {
                            "provider": "akshare_intraday",
                            "status": "failed",
                            "updated": 0,
                            "failed": 1,
                            "failed_reason": "provider down",
                        }
                    ],
                },
            ), mock.patch(
                "quantdesk.real_intraday_sample_validation.simulate_conditional_plan_file",
                return_value={
                    "ok": True,
                    "summary": {"event_count": 0, "no_fill_count": 2, "missing_intraday_count": 1},
                    "events": [],
                    "no_fill": [],
                    "missing_intraday": [],
                },
            ):
                payload = run_real_intraday_sample_validation(
                    symbols="600744",
                    target_date="2026-05-15",
                    provider_order=["akshare_intraday"],
                    intraday_dir=root / "1m",
                    output_path=root / "sample.json",
                    execution_output_path=root / "execution.json",
                    update_output_path=root / "update.json",
                )

        report = PROJECT_ROOT / payload["artifacts"]["report_path"]
        text = report.read_text(encoding="utf-8")
        self.assertIn("未成交/未触发数量", text)
        self.assertIn("缺分钟线次数", text)
        self.assertIn("状态：", text)
        self.assertIn("更新：", text)
        self.assertIn("失败：", text)
        self.assertIn("原因：", text)
        for raw in ("- no_fill：", "- missing_intraday：", "status=", "updated=", "failed=", "reason="):
            self.assertNotIn(raw, text)

    def test_scripts_and_docs_are_wired(self) -> None:
        self.assertTrue((PROJECT_ROOT / "scripts" / "check_intraday_provider_capability.py").exists())
        self.assertTrue((PROJECT_ROOT / "scripts" / "run_real_intraday_sample_validation.py").exists())
        docs = (PROJECT_ROOT / "docs" / "api_payload_schema.md").read_text(encoding="utf-8")
        self.assertIn("2.9.7", docs)


if __name__ == "__main__":
    unittest.main()

