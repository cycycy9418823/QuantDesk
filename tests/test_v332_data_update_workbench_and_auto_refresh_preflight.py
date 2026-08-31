from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DataUpdateWorkbenchPreflightTests(unittest.TestCase):
    def _run_qd_data_update_in_process(self, cli_args: list[str], output_dir: Path, report_path: Path) -> tuple[int, str, dict]:
        from quantdesk import data_update_orchestrator as duo
        from scripts import qd, run_data_update

        fake_update_payload = {
            "status": "completed",
            "provider_results": [{"provider": "mock_provider", "status": "ok"}],
            "updated_symbols": 2,
            "failed_symbols": 0,
            "unchanged_symbols": 0,
            "warnings": [],
            "latest_local_date_after": "2026-05-20",
            "max_local_date_after": "2026-05-20",
        }

        def fake_subprocess_run(cmd: list[str], cwd: Path | None = None, **_kwargs: object) -> subprocess.CompletedProcess:
            script_args = list(cmd[2:])
            with patch.object(duo, "update_daily_prices", return_value=fake_update_payload):
                returncode = run_data_update.main(script_args)
            return subprocess.CompletedProcess(cmd, returncode)

        args = [
            "data-update",
            *cli_args,
            "--auto-health",
            "false",
            "--output-dir",
            str(output_dir),
            "--report-path",
            str(report_path),
        ]
        stdout = StringIO()
        with patch.object(qd.subprocess, "run", side_effect=fake_subprocess_run), redirect_stdout(stdout):
            returncode = qd.main(args)
        payload = json.loads((output_dir / "data_update_latest.json").read_text(encoding="utf-8"))
        return returncode, stdout.getvalue(), payload

    def test_qd_help_mentions_data_update_and_dev_preflight(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/qd.py", "help"],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("data-update", completed.stdout)
        self.assertIn("dev-preflight", completed.stdout)

    def test_data_update_network_false_does_not_update_or_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "data_update"
            report_path = Path(tmp) / "data_update_report.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_data_update.py",
                    "--symbols",
                    "600744,002115",
                    "--target-date",
                    "2026-05-20",
                    "--network",
                    "false",
                    "--output-dir",
                    str(output_dir),
                    "--report-path",
                    str(report_path),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            payload = json.loads((output_dir / "data_update_latest.json").read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertFalse(payload["network"])
        self.assertFalse(payload["update_allowed"])
        self.assertEqual(payload["provider_chain"], [])
        self.assertIn("suggested_next_action", payload)
        self.assertFalse(payload["full_scan"])

    def test_qd_data_update_network_flags_and_dry_run_defaults(self) -> None:
        cases = [
            (["--symbols", "600744,002115", "--target-date", "2026-05-20", "--network", "true"], True, True, False),
            (["--symbols", "600744,002115", "--target-date", "2026-05-20", "--enable-network"], True, True, False),
            (["--symbols", "600744,002115", "--target-date", "2026-05-20", "--network", "false"], False, False, False),
            (["--symbols", "600744,002115", "--target-date", "2026-05-20", "--no-network"], False, False, False),
            (["--symbols", "600744,002115", "--target-date", "2026-05-20", "--network", "true", "--dry-run", "true"], True, False, True),
        ]
        for cli_args, expected_network, expected_update_allowed, expected_dry_run in cases:
            with self.subTest(cli_args=cli_args), tempfile.TemporaryDirectory() as tmp:
                output_dir = Path(tmp) / "data_update"
                report_path = Path(tmp) / "data_update_report.md"
                returncode, stdout, payload = self._run_qd_data_update_in_process(cli_args, output_dir, report_path)
                self.assertEqual(returncode, 0, stdout)
                self.assertEqual(payload["network"], expected_network)
                self.assertEqual(payload["update_allowed"], expected_update_allowed)
                self.assertEqual(payload["dry_run"], expected_dry_run)
                self.assertTrue(stdout.strip())
                self.assertIn("目标完整交易日", stdout)
                self.assertIn(f"network: {str(expected_network).lower()}", stdout)

    def test_network_update_calls_daily_price_engine(self) -> None:
        from quantdesk import data_update_orchestrator as duo

        before_status = {
            "latest_by_symbol": {"600000": "2026-06-18"},
            "latest_local_date": "2026-06-18",
            "max_local_date": "2026-06-18",
            "coverage_date": "2026-06-18",
            "fresh_count": 0,
            "stale_count": 1,
            "missing_count": 0,
            "stale_symbols": ["600000"],
            "missing_symbols": [],
        }
        after_status = {
            "latest_by_symbol": {"600000": "2026-06-23"},
            "latest_local_date": "2026-06-23",
            "max_local_date": "2026-06-23",
            "coverage_date": "2026-06-23",
            "fresh_count": 1,
            "stale_count": 0,
            "missing_count": 0,
            "stale_symbols": [],
            "missing_symbols": [],
        }
        update_payload = {
            "status": "completed",
            "providers": ["tencent_daily"],
            "provider_results": [{"provider": "tencent_daily", "status": "ok"}],
            "updated_symbols": 1,
            "updated_symbol_list": ["600000"],
            "failed_symbols": 0,
            "failed_symbol_list": [],
            "unchanged_symbols": 0,
            "stale_count_after": 0,
            "missing_count_after": 0,
            "latest_local_date_after": "2026-06-23",
            "max_local_date_after": "2026-06-23",
            "warnings": [],
        }
        with patch.object(duo, "daily_price_status", side_effect=[before_status, after_status]), patch.object(
            duo, "update_daily_prices", return_value=update_payload
        ) as runner:
            payload = duo.run_data_update_preflight(
                "600000",
                target_date="2026-06-23",
                network=True,
                auto_update=True,
                max_symbols=1,
                write=False,
            )
        runner.assert_called_once()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["success_count"], 1)
        self.assertEqual(payload["stale_count"], 0)
        self.assertEqual(payload["coverage_date"], "2026-06-23")
        self.assertEqual(payload["provider_chain"], ["tencent_daily"])
        self.assertNotEqual(payload["skipped_reason"], "provider_not_configured")

    def test_readonly_status_uses_persisted_target_suspension(self) -> None:
        from quantdesk import data_update_orchestrator as duo
        from quantdesk.daily_session_evidence import (
            build_baostock_session_evidence,
            write_baostock_session_evidence,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily_dir = root / "data" / "daily"
            metadata_dir = root / "data" / "metadata"
            calendar_dir = root / "data" / "calendar"
            daily_dir.mkdir(parents=True)
            metadata_dir.mkdir(parents=True)
            calendar_dir.mkdir(parents=True)
            (calendar_dir / "a_share_trading_calendar.csv").write_text(
                "date,is_open,source,note\n"
                "2026-07-15,true,test,trading_day\n"
                "2026-07-16,true,test,trading_day\n",
                encoding="utf-8-sig",
            )
            (daily_dir / "600405.csv").write_text(
                "date,symbol,close,adjust\n"
                "2026-07-15,600405,4.20,raw\n",
                encoding="utf-8-sig",
            )
            evidence = build_baostock_session_evidence(
                symbol="600405",
                provider_code="sh.600405",
                query_start_date="2026-07-15",
                query_end_date="2026-07-16",
                records=[
                    {
                        "trade_date": "2026-07-15",
                        "tradestatus": "1",
                    },
                    {
                        "trade_date": "2026-07-16",
                        "tradestatus": "0",
                    },
                ],
                captured_at="2026-07-16T18:00:00+08:00",
            )
            write_baostock_session_evidence(
                evidence,
                metadata_dir=metadata_dir,
            )

            status = duo.daily_price_status(
                ["600405"],
                root=root,
                target_date="2026-07-16",
                max_symbols=1,
            )

        self.assertEqual(status["fresh_count"], 1)
        self.assertEqual(status["stale_count"], 0)
        self.assertEqual(status["coverage_date"], "2026-07-16")
        self.assertEqual(status["raw_bar_coverage_date"], "2026-07-15")
        self.assertEqual(
            status["verified_no_session_symbols"],
            ["600405"],
        )

    def test_target_suspension_does_not_hide_prior_open_session_gap(self) -> None:
        from quantdesk import data_update_orchestrator as duo
        from quantdesk.daily_session_evidence import (
            build_baostock_session_evidence,
            write_baostock_session_evidence,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily_dir = root / "data" / "daily"
            metadata_dir = root / "data" / "metadata"
            calendar_dir = root / "data" / "calendar"
            daily_dir.mkdir(parents=True)
            metadata_dir.mkdir(parents=True)
            calendar_dir.mkdir(parents=True)
            (daily_dir / "600405.csv").write_text(
                "date,symbol,close,adjust\n"
                "2026-07-10,600405,4.20,raw\n",
                encoding="utf-8-sig",
            )
            (calendar_dir / "a_share_trading_calendar.csv").write_text(
                "date,is_open,source,note\n"
                "2026-07-10,true,test,trading_day\n"
                "2026-07-13,true,test,trading_day\n"
                "2026-07-14,true,test,trading_day\n"
                "2026-07-15,true,test,trading_day\n"
                "2026-07-16,true,test,trading_day\n",
                encoding="utf-8-sig",
            )
            evidence = build_baostock_session_evidence(
                symbol="600405",
                provider_code="sh.600405",
                query_start_date="2026-07-16",
                query_end_date="2026-07-16",
                records=[
                    {
                        "trade_date": "2026-07-16",
                        "tradestatus": "0",
                    },
                ],
                captured_at="2026-07-16T18:00:00+08:00",
            )
            write_baostock_session_evidence(
                evidence,
                metadata_dir=metadata_dir,
            )

            status = duo.daily_price_status(
                ["600405"],
                root=root,
                target_date="2026-07-16",
                max_symbols=1,
            )

        self.assertEqual(status["fresh_count"], 0)
        self.assertEqual(status["stale_count"], 1)
        self.assertEqual(
            status["missing_recent_trade_dates_by_symbol"],
            {
                "600405": [
                    "2026-07-13",
                    "2026-07-14",
                    "2026-07-15",
                ]
            },
        )
        self.assertEqual(status["coverage_date"], "2026-07-10")

    def test_zero_universe_never_inherits_global_coverage(self) -> None:
        from quantdesk import data_update_orchestrator as duo

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "daily").mkdir(parents=True)
            with patch.object(
                duo,
                "resolve_symbols",
                return_value=[],
            ), patch.object(
                duo,
                "freshness_check",
                return_value={
                    "latest_trade_date_in_daily": "2026-07-16",
                },
            ):
                payload = duo.run_data_update_preflight(
                    root,
                    target_date="2026-07-16",
                    network=False,
                    auto_update=False,
                    max_symbols=2,
                    write=False,
                )

        self.assertEqual(payload["selected_count"], 0)
        self.assertEqual(payload["coverage_date"], "")
        self.assertEqual(payload["latest_local_daily_date"], "")
        self.assertEqual(payload["data_freshness_status"], "no_universe")
        self.assertFalse(payload["full_pool_coverage"])

    def test_max_symbols_marks_partial_pool_scope(self) -> None:
        from quantdesk import data_update_orchestrator as duo

        status = {
            "latest_by_symbol": {
                "600000": "2026-07-16",
                "600001": "2026-07-16",
            },
            "latest_local_date": "2026-07-16",
            "max_local_date": "2026-07-16",
            "coverage_date": "2026-07-16",
            "fresh_count": 2,
            "stale_count": 0,
            "missing_count": 0,
            "fresh_symbols": ["600000", "600001"],
            "stale_symbols": [],
            "missing_symbols": [],
            "data_freshness_status": "fresh",
        }
        with patch.object(
            duo,
            "daily_price_status",
            return_value=status,
        ), patch.object(
            duo,
            "freshness_check",
            return_value={
                "latest_trade_date_in_daily": "2026-07-16",
            },
        ):
            payload = duo.run_data_update_preflight(
                "600000,600001,600002",
                target_date="2026-07-16",
                network=False,
                auto_update=False,
                max_symbols=2,
                write=False,
            )

        self.assertEqual(payload["requested_count"], 3)
        self.assertEqual(payload["selected_count"], 2)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["coverage_scope"], "partial_pool")
        self.assertFalse(payload["full_pool_coverage"])

    def test_missing_symbols_are_not_subtracted_twice_from_fresh_count(self) -> None:
        from quantdesk import data_update_orchestrator as duo

        before_status = {
            "latest_by_symbol": {"600000": "2026-07-15"},
            "latest_local_date": "2026-07-15",
            "max_local_date": "2026-07-15",
            "coverage_date": "",
            "fresh_count": 0,
            "stale_count": 2,
            "missing_count": 1,
            "stale_symbols": ["600000", "600001"],
            "missing_symbols": ["600001"],
        }
        after_status = {
            "latest_by_symbol": {"600000": "2026-07-16"},
            "latest_local_date": "2026-07-16",
            "max_local_date": "2026-07-16",
            "coverage_date": "",
            "fresh_count": 1,
            "stale_count": 1,
            "missing_count": 1,
            "stale_symbols": ["600001"],
            "missing_symbols": ["600001"],
        }
        update_payload = {
            "status": "partial",
            "providers": ["mock"],
            "provider_results": [],
            "updated_symbols": 1,
            "updated_symbol_list": ["600000"],
            "unchanged_symbols": 0,
            "failed_symbols": 1,
            "failed_symbol_list": ["600001"],
            "stale_count_after": 1,
            "missing_count_after": 1,
            "fresh_symbol_list_after": ["600000"],
            "stale_symbol_list_after": ["600001"],
            "missing_symbol_list_after": ["600001"],
            "coverage_date_after": "",
            "warnings": [],
        }

        with patch.object(
            duo,
            "daily_price_status",
            side_effect=[before_status, after_status],
        ), patch.object(
            duo,
            "update_daily_prices",
            return_value=update_payload,
        ):
            payload = duo.run_data_update_preflight(
                "600000,600001",
                target_date="2026-07-16",
                network=True,
                auto_update=True,
                max_symbols=2,
                write=False,
            )

        self.assertEqual(payload["fresh_count"], 1)
        self.assertEqual(payload["stale_count"], 1)
        self.assertEqual(payload["missing_count"], 1)
        self.assertEqual(payload["stale_symbols"], ["600001"])
        self.assertEqual(payload["missing_symbols"], ["600001"])

    def test_verified_target_suspension_uses_engine_coverage_and_symbol_lists(self) -> None:
        from quantdesk import data_update_orchestrator as duo

        before_status = {
            "latest_by_symbol": {
                "600000": "2026-07-15",
                "600405": "2026-07-15",
            },
            "latest_local_date": "2026-07-15",
            "max_local_date": "2026-07-15",
            "coverage_date": "2026-07-15",
            "fresh_count": 0,
            "stale_count": 2,
            "missing_count": 0,
            "stale_symbols": ["600000", "600405"],
            "missing_symbols": [],
        }
        legacy_after_status = {
            "latest_by_symbol": {
                "600000": "2026-07-16",
                "600405": "2026-07-15",
            },
            "latest_local_date": "2026-07-15",
            "max_local_date": "2026-07-16",
            "coverage_date": "2026-07-15",
            "fresh_count": 1,
            "stale_count": 1,
            "missing_count": 0,
            "stale_symbols": ["600405"],
            "missing_symbols": [],
        }
        update_payload = {
            "status": "completed",
            "providers": ["baostock_daily"],
            "provider_results": [],
            "updated_symbols": 1,
            "updated_symbol_list": ["600000"],
            "unchanged_symbols": 1,
            "failed_symbols": 0,
            "failed_symbol_list": [],
            "stale_count_after": 0,
            "missing_count_after": 0,
            "fresh_symbol_list_after": ["600000", "600405"],
            "stale_symbol_list_after": [],
            "missing_symbol_list_after": [],
            "verified_no_session_symbols": 1,
            "verified_no_session_symbol_list": ["600405"],
            "latest_local_date_after": "2026-07-15",
            "max_local_date_after": "2026-07-16",
            "coverage_date_after": "2026-07-16",
            "raw_bar_coverage_date_after": "2026-07-15",
            "warnings": [],
        }

        with patch.object(
            duo,
            "daily_price_status",
            side_effect=[before_status, legacy_after_status],
        ), patch.object(
            duo,
            "update_daily_prices",
            return_value=update_payload,
        ):
            payload = duo.run_data_update_preflight(
                "600000,600405",
                target_date="2026-07-16",
                network=True,
                auto_update=True,
                max_symbols=2,
                write=False,
            )

        self.assertEqual(payload["fresh_count"], 2)
        self.assertEqual(payload["stale_count"], 0)
        self.assertEqual(payload["coverage_date"], "2026-07-16")
        self.assertEqual(payload["raw_bar_coverage_date"], "2026-07-15")
        self.assertEqual(payload["stale_symbols"], [])
        self.assertEqual(
            payload["verified_no_session_symbols"],
            ["600405"],
        )

    def test_qd_data_update_no_network_wins_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "data_update"
            report_path = Path(tmp) / "data_update_report.md"
            _returncode, stdout, payload = self._run_qd_data_update_in_process(
                ["--symbols", "600744,002115", "--target-date", "2026-05-20", "--network", "true", "--no-network"],
                output_dir,
                report_path,
            )
        self.assertFalse(payload["network"])
        self.assertFalse(payload["update_allowed"])
        self.assertTrue(any("--no-network" in item for item in payload["warnings"]))
        self.assertIn("network: false", stdout)

    def test_data_update_json_is_utf8_without_mojibake_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "data_update"
            report_path = Path(tmp) / "data_update_report.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_data_update.py",
                    "--symbols",
                    "600744,002115",
                    "--target-date",
                    "2026-05-20",
                    "--network",
                    "false",
                    "--output-dir",
                    str(output_dir),
                    "--report-path",
                    str(report_path),
                ],
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            raw = (output_dir / "data_update_latest.json").read_text(encoding="utf-8")
            payload = json.loads(raw)
        stderr = (completed.stderr or b"").decode(errors="replace")
        stdout = (completed.stdout or b"").decode(errors="replace")
        self.assertEqual(completed.returncode, 0, stderr + stdout)
        for marker in ("锛", "涓", "鐩", "鏃", "鏈"):
            self.assertNotIn(marker, raw)
        text_fields = "\n".join(
            [*payload.get("warnings", []), payload.get("skipped_reason", ""), payload.get("disclaimer", "")]
        )
        for marker in ("锛", "涓", "鐩", "鏃", "鏈"):
            self.assertNotIn(marker, text_fields)

    def test_missing_daily_cache_is_warning_not_failure(self) -> None:
        from quantdesk import daily_price_update_engine
        from quantdesk import data_update_orchestrator as duo

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_daily = root / "data" / "daily"
            with patch.object(duo, "PROJECT_ROOT", root), patch.object(daily_price_update_engine, "DAILY_DIR", missing_daily):
                payload = duo.run_data_update_preflight(
                    "600744",
                    target_date="2026-05-20",
                    network=False,
                    write=False,
                )
        self.assertEqual(payload["status"], "warning")
        self.assertFalse(payload["daily_cache_exists"])
        self.assertTrue(any("data/daily" in item for item in payload["warnings"]))

    def test_daily_price_status_uses_symbol_level_latest_dates(self) -> None:
        from quantdesk import data_update_orchestrator as duo

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = root / "data" / "daily"
            daily.mkdir(parents=True)
            (daily / "600000.csv").write_text(
                "date,symbol,close\n2026-06-04,600000,9.19\n2026-06-05,600000,9.34\n",
                encoding="utf-8",
            )
            (daily / "000001.csv").write_text(
                "date,symbol,close\n2026-06-08,000001,12.1\n2026-06-09,000001,12.2\n",
                encoding="utf-8",
            )
            payload = duo.daily_price_status(
                ["600000", "000001"],
                root=root,
                target_date="2026-06-10",
                max_symbols=10,
            )
        self.assertEqual(payload["latest_by_symbol"]["600000"], "2026-06-05")
        self.assertEqual(payload["latest_by_symbol"]["000001"], "2026-06-09")
        self.assertEqual(payload["coverage_date"], "2026-06-05")
        self.assertEqual(payload["max_local_date"], "2026-06-09")
        self.assertEqual(payload["stale_count"], 2)

    def test_max_symbols_limits_current_analysis_pool_scope(self) -> None:
        from quantdesk import data_update_orchestrator as duo

        rows = [{"symbol": f"{idx:06d}", "name": str(idx), "included": "true"} for idx in range(100)]
        with patch.object(duo, "resolve_symbols", return_value=rows):
            selected = duo.select_update_symbols(None, max_symbols=7)
        self.assertEqual(selected["symbol_count"], 7)
        self.assertEqual(len(selected["symbols"]), 7)
        self.assertEqual(selected["symbol_source"], "current_analysis_pool")
        self.assertFalse(selected["full_scan"])

    def test_data_update_builder_never_defaults_to_network(self) -> None:
        """Split from the retired-route half; this is the live safety guard.

        The old method also asserted /api/workbench/data-update route strings inside
        scripts/quant_app_server.py. Those routes never migrated - the active
        analogue is POST /api/data/update-all, whose explicit-network gate is pinned
        by tests/test_r4_data_update_route_safety.py. What must survive here is the
        builder contract: it reports readiness WITHOUT turning the network on.
        """
        from quantdesk.data_update_orchestrator import build_workbench_data_update

        payload = build_workbench_data_update(project_root=PROJECT_ROOT)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["network_default"], "builder must not default to network")
        self.assertIn(payload["status"], {"passed", "warning", "missing"})
        self.assertIn("suggested_commands", payload)

    def test_active_desk_separates_data_update_from_data_health(self) -> None:
        """A user must be able to tell "fetch data" from "check data".

        Re-anchored from the retired index.html / app.js. The old tab-wiring tokens
        (data-primary-tab="dataUpdateWorkbench", dataUpdateWorkbenchSection) and the
        two long Chinese explanations do not exist on the active desk, so this
        asserts the distinction through what the active desk does carry: both labels
        plus two distinct endpoints.
        """
        html = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(
            encoding="utf-8"
        )
        bundle = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(
            encoding="utf-8"
        )
        desk = html + bundle
        self.assertIn("数据更新", desk)
        self.assertIn("数据健康", desk)
        self.assertIn("/api/data/update-all", bundle)
        self.assertIn("/api/data-health/dashboard", bundle)

    def test_daily_retain_snapshot_records_stale_data_warning(self) -> None:
        from quantdesk.daily_data_retention import collect_daily_retention_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            snapshot = collect_daily_retention_snapshot(
                {
                    "target_date": "2026-05-20",
                    "symbols": ["600744"],
                    "network": False,
                    "llm_enabled": False,
                    "include_provider_smoke": False,
                    "include_strategy_validation": False,
                    "include_observation_report": False,
                    "day_dir": str(Path(tmp) / "daily_retention"),
                    "max_symbols": 1,
                    "warnings": [],
                }
            )
        self.assertIn("stale_data_warning", snapshot)
        self.assertIn("stale_data_warning", snapshot["manifest"])
        self.assertIn(snapshot["stale_data_warning"]["status"], {"passed", "warning"})

    def test_observation_context_records_stale_data_warning(self) -> None:
        from quantdesk.strategy_observation_contract import build_strategy_observation_context

        context = build_strategy_observation_context(
            symbol="600744",
            target_date="2026-05-20",
            network=False,
            data_health_payload={"status": "warning", "warnings": []},
            provider_payload={"status": "skipped"},
            strategy_payload={"status": "missing"},
            workflow_payload={"status": "missing"},
            conditional_plan_payload={"status": "missing"},
            execution_payload={"status": "missing"},
            intraday_payload={"status": "missing"},
            portfolio_payload={},
        )
        self.assertIn("stale_data_warning", context)
        self.assertIn(context["stale_data_warning"]["status"], {"passed", "warning"})

    def test_archive_plan_and_clean_zip_rules_include_data_update_exclusion(self) -> None:
        from quantdesk.result_archive import build_archive_plan

        plan = build_archive_plan(target_date="2026-05-20", dry_run=True)
        sources = "\n".join(item["source"] for item in plan["entries"])
        self.assertIn("results/data_update/data_update_latest.json", sources)
        self.assertIn("reports/data_update_report_latest.md", sources)
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8")
        self.assertIn('"results"', export_text)
        self.assertIn('"reports"', export_text)
        zip_path = PROJECT_ROOT / "dist" / "quantdesk_clean.zip"
        if zip_path.exists():
            with __import__("zipfile").ZipFile(zip_path) as zf:
                names = zf.namelist()
            self.assertFalse(any(name.startswith("results/data_update/") for name in names))
            self.assertFalse(any(name == "reports/data_update_report_latest.md" for name in names))


    def test_v331_test_file_still_present(self) -> None:
        self.assertTrue((PROJECT_ROOT / "tests" / "test_v331_workbench_ui_and_workflow_unification.py").exists())


if __name__ == "__main__":
    unittest.main()


