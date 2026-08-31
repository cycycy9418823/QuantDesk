from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from quantdesk.intraday.intraday_gap_checker import collect_intraday_required_symbols, intraday_gap_status
from quantdesk.intraday.intraday_import import import_intraday_1m_file
from quantdesk.intraday.intraday_schema import normalize_intraday_frame, read_intraday_csv
from quantdesk.paper.position_management_engine import (
    apply_conditional_execution_to_state,
    simulate_conditional_plan,
    simulate_conditional_plan_file,
)
from scripts.import_intraday_1m import build_parser as build_intraday_import_parser
from scripts import generate_intraday_1m_import_template as intraday_import_template

ROOT = Path(__file__).resolve().parents[1]


def _new_plan(symbol: str = "002115") -> dict:
    return {
        "plan_id": "plan_test",
        "symbol": symbol,
        "name": "测试股票",
        "strategy_type": "long",
        "strategy_label": "长线",
        "position_state": "new_candidate",
        "trade_date": "2026-05-18",
        "no_chase_above": 12.0,
        "rules": {
            "risk_guards": [{"guard": "no_chase", "trigger_type": "too_high_to_buy", "price_above": 12.0}],
            "entry_rules": [
                {"action": "buy", "trigger_type": "pullback_buy", "price_low": 10.0, "price_high": 10.5, "quantity": 100, "reason": "测试低吸"},
                {"action": "buy", "trigger_type": "breakout_buy", "price_low": 11.0, "price_high": 12.0, "quantity": 100, "reason": "测试突破"},
            ],
        },
    }


def _holding_plan(action: str = "take_profit") -> dict:
    return {
        "plan_id": "plan_test",
        "symbol": "600744",
        "name": "华银电力",
        "strategy_type": "long",
        "strategy_label": "长线",
        "position_state": "holding",
        "trade_date": "2026-05-18",
        "current_quantity": 200,
        "sellable_quantity": 200,
        "rules": {
            "risk_guards": [],
            "add_rules": [{"action": "add", "trigger_type": "pullback_add", "price_low": 9.8, "price_high": 10.0, "quantity": 100}],
            "exit_rules": [
                {"action": "sell_all", "trigger_type": "stop_loss", "price_low": 0, "price_high": 9.0, "quantity_pct": 100},
                {"action": "reduce_half", "trigger_type": "take_profit_half", "price_low": 11.0, "price_high": 12.0, "quantity_pct": 50},
            ],
        },
    }


def _bars(symbol: str, prices: list[float], date: str = "2026-05-18") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "datetime": f"{date} 09:{31 + i:02d}:00",
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 1000,
                "amount": price * 1000,
            }
            for i, price in enumerate(prices)
        ]
    )


class V293IntradayImportAndExecutionClosureTests(unittest.TestCase):
    def test_intraday_schema_reads_english_columns(self) -> None:
        payload = normalize_intraday_frame(_bars("002115", [10.1, 10.2]))
        self.assertTrue(payload["ok"])
        frame = payload["frame"]
        self.assertEqual(frame.iloc[0]["symbol"], "002115")
        self.assertEqual(frame.iloc[0]["date"], "2026-05-18")

    def test_intraday_schema_reads_chinese_columns(self) -> None:
        raw = pd.DataFrame([
            {"代码": "002115", "时间": "2026-05-18 09:31:00", "开盘": 10.1, "最高": 10.2, "最低": 10.0, "收盘": 10.15, "成交量": 1, "成交额": 10.15}
        ])
        payload = normalize_intraday_frame(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["frame"].iloc[0]["symbol"], "002115")
        self.assertEqual(payload["frame"].iloc[0]["date"], "2026-05-18")

    def test_intraday_schema_extracts_date_from_datetime_for_target_filter(self) -> None:
        raw = pd.DataFrame([
            {"symbol": "002115", "datetime": "2026-05-18 09:31:00", "open": 10.1, "high": 10.2, "low": 10.0, "close": 10.15},
            {"symbol": "002115", "datetime": "2026-05-19 09:31:00", "open": 10.3, "high": 10.4, "low": 10.2, "close": 10.35},
        ])
        payload = normalize_intraday_frame(raw, target_date="2026-05-18")
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["frame"]), 1)
        self.assertEqual(payload["frame"].iloc[0]["date"], "2026-05-18")
        self.assertEqual(payload["frame"].iloc[0]["symbol"], "002115")
        self.assertEqual(payload["source_row_count"], 2)
        self.assertEqual(payload["latest_available_date"], "2026-05-19")
        self.assertEqual(payload["latest_available_datetime"], "2026-05-19 09:31:00")

    def test_symbol_prefix_zero_is_preserved(self) -> None:
        payload = normalize_intraday_frame(pd.DataFrame([{"code": 2115, "datetime": "2026-05-18 09:31:00", "price": 10.0}]))
        self.assertEqual(payload["frame"].iloc[0]["symbol"], "002115")
        self.assertTrue(payload["price_only_intraday"])

    def test_read_intraday_csv_skips_bad_rows_with_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "bad_intraday.csv"
            src.write_text(
                "\n".join(
                    [
                        "symbol,datetime,open,high,low,close,volume,amount",
                        "002115,2026-05-18 09:31:00,10.1,10.2,10.0,10.15,1000,10150",
                        "bad,row,with,too,many,columns,for,this,csv,boom",
                        "002115,2026-05-18 09:32:00,10.2,10.3,10.1,10.25,1200,12300",
                    ]
                ),
                encoding="utf-8-sig",
            )

            payload = read_intraday_csv(src, default_symbol="002115")

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["row_count"], 2)
            self.assertTrue(any("csv_bad_lines_skipped" in item for item in payload["warnings"]))

    def test_import_writes_intraday_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "input.csv"
            _bars("002115", [10.1, 10.2]).to_csv(src, index=False, encoding="utf-8-sig")
            out = import_intraday_1m_file(src, base_dir=root / "1m", output_path=root / "result.json")
            self.assertTrue(out["ok"])
            self.assertTrue((root / "1m" / "002115.csv").exists())

    def test_duplicate_import_deduplicates_datetime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "input.csv"
            pd.concat([_bars("002115", [10.1]), _bars("002115", [10.2])], ignore_index=True).to_csv(src, index=False, encoding="utf-8-sig")
            import_intraday_1m_file(src, base_dir=root / "1m", output_path=root / "result.json")
            import_intraday_1m_file(src, base_dir=root / "1m", output_path=root / "result.json")
            saved = pd.read_csv(root / "1m" / "002115.csv", encoding="utf-8-sig")
            self.assertEqual(len(saved), 1)

    def test_import_skips_bad_csv_rows_with_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "bad_import.csv"
            src.write_text(
                "\n".join(
                    [
                        "symbol,datetime,open,high,low,close,volume,amount",
                        "002115,2026-05-18 09:31:00,10.1,10.2,10.0,10.15,1000,10150",
                        "bad,row,with,too,many,columns,for,this,csv,boom",
                        "002115,2026-05-18 09:32:00,10.2,10.3,10.1,10.25,1200,12300",
                    ]
                ),
                encoding="utf-8-sig",
            )

            out = import_intraday_1m_file(src, base_dir=root / "1m", output_path=root / "result.json")

            self.assertTrue(out["ok"])
            self.assertEqual(out["imported_rows"], 2)
            self.assertTrue(any("csv_bad_lines_skipped" in item for item in out["warnings"]))

    def test_validate_only_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "input.csv"
            _bars("002115", [10.1]).to_csv(src, index=False, encoding="utf-8-sig")
            out = import_intraday_1m_file(src, base_dir=root / "1m", validate_only=True, output_path=root / "result.json")
            self.assertTrue(out["ok"])
            self.assertFalse((root / "1m" / "002115.csv").exists())

    def test_import_cli_can_disable_append(self) -> None:
        parser = build_intraday_import_parser()
        default_args = parser.parse_args(["--input", "demo.csv"])
        disabled_args = parser.parse_args(["--input", "demo.csv", "--no-append"])

        self.assertTrue(default_args.append)
        self.assertFalse(disabled_args.append)

    def test_generate_current_import_template_for_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(intraday_import_template, "write_json") as write_json:
                out = intraday_import_template.generate_template(
                    source="symbols",
                    symbols="002115,600744",
                    output=root / "template.csv",
                    overwrite=True,
                )
            self.assertTrue(out["ok"])
            text = (root / "template.csv").read_text(encoding="utf-8-sig")
            self.assertIn("002115", text)
            self.assertNotIn("说明行", text)
            write_json.assert_called_once()

    def test_generate_template_keeps_readme_with_custom_output(self) -> None:
        with tempfile.TemporaryDirectory() as output_tmp, tempfile.TemporaryDirectory() as protected_tmp:
            output = Path(output_tmp) / "template.csv"
            simulated_protected_dir = Path(protected_tmp)
            protected_readme = simulated_protected_dir / "README_intraday_import.md"
            sentinel = "protected sentinel"
            protected_readme.write_text(sentinel, encoding="utf-8")

            with mock.patch.object(
                intraday_import_template,
                "INTRADAY_IMPORT_DIR",
                simulated_protected_dir,
                create=True,
            ):
                with mock.patch.object(intraday_import_template, "write_json") as write_json:
                    payload = intraday_import_template.generate_template(
                        source="symbols",
                        symbols="002115",
                        output=output,
                        overwrite=True,
                    )

            companion_readme = output.parent / "README_intraday_import.md"
            self.assertEqual(protected_readme.read_text(encoding="utf-8"), sentinel)
            self.assertEqual(Path(payload["readme_path"]), companion_readme)
            self.assertTrue(companion_readme.is_file())
            write_json.assert_called_once()

    def test_generate_template_preserves_existing_readme_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "template.csv"
            companion_readme = root / "README_intraday_import.md"
            sentinel = "existing companion readme"
            companion_readme.write_text(sentinel, encoding="utf-8")

            with mock.patch.object(intraday_import_template, "write_json") as write_json:
                payload = intraday_import_template.generate_template(
                    source="symbols",
                    symbols="002115",
                    output=output,
                    overwrite=False,
                )

            self.assertTrue(output.is_file())
            self.assertEqual(companion_readme.read_text(encoding="utf-8"), sentinel)
            self.assertEqual(Path(payload["readme_path"]), companion_readme)
            write_json.assert_called_once()

    def test_gap_checker_missing_and_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = root / "results"
            state = root / "state"
            intraday = root / "data" / "intraday" / "1m"
            results.mkdir(parents=True)
            state.mkdir(parents=True)
            intraday.mkdir(parents=True)
            (results / "conditional_trade_plans_latest.json").write_text(json.dumps({"plan_id": "p", "trade_date": "2026-05-18", "symbols": [{"symbol": "002115"}, {"symbol": "600744"}]}), encoding="utf-8")
            _bars("002115", [10.1, 10.2]).to_csv(intraday / "002115.csv", index=False, encoding="utf-8-sig")
            with mock.patch("quantdesk.intraday_gap_checker.CONDITIONAL_TRADE_PLANS_LATEST_JSON", results / "conditional_trade_plans_latest.json"):
                status = intraday_gap_status(target_trade_date="2026-05-18", source="plans", intraday_dir=intraday)
            self.assertIn("002115", status["available_symbols"])
            self.assertIn("600744", status["missing_symbols"])

    def test_collect_required_symbols_uses_late_bound_patched_plan_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "conditional_trade_plans_latest.json"
            plan_path.write_text(
                json.dumps({"plan_id": "p", "trade_date": "2026-05-18", "symbols": [{"symbol": "002115"}]}),
                encoding="utf-8",
            )
            with mock.patch("quantdesk.intraday_gap_checker.CONDITIONAL_TRADE_PLANS_LATEST_JSON", plan_path):
                collected = collect_intraday_required_symbols(source="plans")
            self.assertTrue(collected["plan_exists"])
            self.assertEqual(collected["target_trade_date"], "2026-05-18")
            self.assertEqual(collected["items"][0]["symbol"], "002115")

    def test_collect_required_symbols_explicit_plan_path_and_clean_zip_missing_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps({"plan_id": "p", "trade_date": "2026-05-18", "symbols": [{"symbol": "002115"}]}),
                encoding="utf-8",
            )
            collected = collect_intraday_required_symbols(source="plans", plan_path=plan_path)
            self.assertEqual([row["symbol"] for row in collected["items"]], ["002115"])

            missing = collect_intraday_required_symbols(
                source="plans",
                plan_path=root / "results" / "conditional_trade_plans_latest.json",
                state_path=root / "state" / "auto_paper_portfolio.json",
                strategy_path=root / "results" / "auto_strategy_latest.json",
                portfolio_latest_path=root / "results" / "auto_paper_portfolio_latest.json",
                workflow_path=root / "results" / "daily_workflow_latest.json",
            )
            self.assertFalse(missing["plan_exists"])
            self.assertEqual(missing["items"], [])

    def test_gap_checker_available_with_002115_and_explicit_plan_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "conditional_trade_plans_latest.json"
            intraday = root / "intraday"
            intraday.mkdir()
            plan_path.write_text(
                json.dumps({"plan_id": "p", "trade_date": "2026-05-18", "symbols": [{"symbol": "002115"}, {"symbol": "600744"}]}),
                encoding="utf-8",
            )
            _bars("002115", [10.1, 10.2]).to_csv(intraday / "002115.csv", index=False, encoding="utf-8-sig")
            status = intraday_gap_status(target_trade_date="2026-05-18", source="plans", intraday_dir=intraday, plan_path=plan_path)
            self.assertIn("002115", status["available_symbols"])
            self.assertIn("600744", status["missing_symbols"])

    def test_missing_intraday_generates_missing_without_fake_fill(self) -> None:
        result = simulate_conditional_plan(_new_plan(), None)
        self.assertEqual(result["action"], "no_fill")
        self.assertEqual(result["fill_model"], "missing_intraday")
        self.assertFalse(result["is_reliable"])
        self.assertIn("不会用收盘价假装成交", result["reason"])

    def test_intraday_no_trigger_generates_no_fill(self) -> None:
        result = simulate_conditional_plan(_new_plan(), _bars("002115", [9.5, 9.6]))
        self.assertEqual(result["action"], "no_fill")
        self.assertEqual(result["fill_model"], "intraday_1m_close")
        self.assertTrue(result["is_reliable"])

    def test_high_low_touch_does_not_fake_fill_when_close_not_in_zone(self) -> None:
        bars = _bars("002115", [9.8])
        bars.loc[0, "high"] = 10.3
        result = simulate_conditional_plan(_new_plan(), bars)
        self.assertEqual(result["action"], "no_fill")

    def test_intraday_pullback_triggers_buy(self) -> None:
        result = simulate_conditional_plan(_new_plan(), _bars("002115", [9.8, 10.2]))
        self.assertEqual(result["action"], "buy")
        self.assertEqual(result["trigger_time"], "2026-05-18 09:32:00")
        self.assertEqual(result["trigger_price"], 10.2)

    def test_intraday_triggers_take_profit(self) -> None:
        result = simulate_conditional_plan(_holding_plan(), _bars("600744", [10.2, 11.2]))
        self.assertIn(result["action"], {"reduce_half", "take_profit"})
        self.assertEqual(result["quantity"], 100)

    def test_intraday_triggers_stop_loss_before_add(self) -> None:
        result = simulate_conditional_plan(_holding_plan(), _bars("600744", [8.8, 9.9]))
        self.assertIn(result["action"], {"stop_loss", "sell_all"})
        self.assertEqual(result["trigger_price"], 8.8)

    def test_minute_close_is_default_trigger_model(self) -> None:
        result = simulate_conditional_plan(_new_plan(), _bars("002115", [10.2]), trigger_model="minute_bar_touch")
        self.assertEqual(result["fill_model"], "intraday_1m_close")

    def test_missing_intraday_does_not_change_cash_or_positions(self) -> None:
        state = {"cash": 100000, "initial_cash": 100000, "positions": []}
        execution = {"plan_id": "p", "trade_date": "2026-05-18", "events": [], "no_fill": [simulate_conditional_plan(_new_plan(), None)], "blocked": []}
        applied = apply_conditional_execution_to_state(state, execution, daily_dir=Path("missing"))
        self.assertEqual(applied["state"]["cash"], 100000)
        self.assertEqual(applied["state"]["positions"], [])

    def test_reliable_event_updates_portfolio_and_daily_actions(self) -> None:
        state = {"cash": 100000, "initial_cash": 100000, "positions": []}
        event = simulate_conditional_plan(_new_plan(), _bars("002115", [10.2]))
        execution = {"plan_id": "p2", "trade_date": "2026-05-18", "events": [event], "no_fill": [], "blocked": []}
        applied = apply_conditional_execution_to_state(state, execution, daily_dir=Path("missing"))
        self.assertEqual(applied["state"]["positions"][0]["symbol"], "002115")
        action = applied["state"]["daily_actions"][0]
        self.assertEqual(action["fill_model"], "intraday_1m_close")
        self.assertTrue(action["is_reliable"])
        self.assertEqual(action["trigger_time"], "2026-05-18 09:31:00")

    def test_simulate_conditional_plan_file_writes_missing_and_available_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = {
                "plan_id": "p3",
                "plan_date": "2026-05-17",
                "trade_date": "2026-05-18",
                "symbols": [_new_plan("002115"), _new_plan("600744")],
            }
            intraday = root / "data" / "intraday"
            (intraday / "1m").mkdir(parents=True)
            _bars("002115", [10.2]).to_csv(intraday / "1m" / "002115.csv", index=False, encoding="utf-8-sig")
            out = simulate_conditional_plan_file(plan, intraday_dir=intraday, output_path=root / "execution.json")
            self.assertEqual(out["summary"]["plan_count"], 2)
            self.assertEqual(out["summary"]["available_intraday_count"], 1)
            self.assertEqual(out["summary"]["missing_intraday_count"], 1)
            self.assertEqual(len(out["missing_intraday"]), 1)

    def test_simulate_conditional_plan_file_exposes_intraday_csv_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = {
                "plan_id": "p_bad_csv",
                "plan_date": "2026-05-17",
                "trade_date": "2026-05-18",
                "symbols": [_new_plan("002115")],
            }
            intraday = root / "data" / "intraday"
            (intraday / "1m").mkdir(parents=True)
            (intraday / "1m" / "002115.csv").write_text(
                "symbol,datetime,open,high,low,close,volume,amount\n"
                "002115,2026-05-18 09:31:00,10.2,10.2,10.2,10.2,1000,10200\n"
                "002115,2026-05-18 09:32:00,10.3,10.3,10.3,10.3,1000,10300,unexpected_extra_field\n",
                encoding="utf-8",
            )

            out = simulate_conditional_plan_file(plan, intraday_dir=intraday, output_path=root / "execution.json")

            self.assertEqual(out["summary"]["available_intraday_count"], 1)
            self.assertTrue(any("csv_bad_lines_skipped" in item for item in out["warnings"]))

    def test_conditional_execution_schema_version_uses_current_api(self) -> None:
        out = simulate_conditional_plan_file({"plan_id": "p4", "trade_date": "2026-05-18", "symbols": [_new_plan()]}, write=False)
        self.assertIn(out["schema_version"], {"2.9.3", "2.9.4", "2.9.5", "2.9.6", "2.9.7", "2.9.8", "2.9.9", "3.0.0", "3.0.1", "3.0.2", "3.0.3", "3.0.4", "3.1.0", "3.1.1", "3.1.2", "3.1.3", "3.2.0", "3.2.1", "3.2.2", "3.2.3", "3.2.4", "3.2.5", "3.2.6", "3.2.6.1", "3.2.7", "3.2.8", "3.2.9", "3.3.0", "3.3.1", "3.3.2", "3.3.2.1", "3.3.2.2", "3.3.2.3", "3.3.2.4", "3.3.2.5", "3.3.3", "3.3.4", "3.3.5", "3.3.6", "3.3.7", "3.4.0", "3.4.1", "3.4.2", "3.4.2.1", "3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})


    def test_import_script_supports_new_input_flags(self) -> None:
        text = (ROOT / "scripts" / "import_intraday_1m.py").read_text(encoding="utf-8")
        self.assertIn("--input", text)
        self.assertIn("--validate-only", text)

    def test_daily_workflow_contains_intraday_status_summary(self) -> None:
        text = (ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("intraday_status", text)
        self.assertIn("intraday_gap_check", text)

    def test_default_path_binding_guard_is_wired(self) -> None:
        gap_path = ROOT / "quantdesk" / "intraday_gap_checker.py"
        if not gap_path.exists():
            gap_path = ROOT / "quantdesk" / "intraday" / "intraday_gap_checker.py"
        gap_text = gap_path.read_text(encoding="utf-8")
        self.assertNotIn("plan_path: Path = CONDITIONAL_TRADE_PLANS_LATEST_JSON", gap_text)
        self.assertIn("plan_path: Path | None = None", gap_text)
        self.assertIn("plan_path = Path(plan_path or CONDITIONAL_TRADE_PLANS_LATEST_JSON)", gap_text)
        self_check_text = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("default_path_binding_guard", self_check_text)


    def test_report_contains_daily_intraday_boundary_phrase(self) -> None:
        text = (ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("完整日线控制收益复盘；分钟线控制盘中触发验证", text)


if __name__ == "__main__":
    unittest.main()


