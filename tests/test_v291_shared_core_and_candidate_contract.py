from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SharedCoreAndCandidateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_root = PROJECT_ROOT / ".tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.tmp_obj = tempfile.TemporaryDirectory(dir=tmp_root)
        self.tmp = Path(self.tmp_obj.name)

    def tearDown(self) -> None:
        self.tmp_obj.cleanup()

    def test_normalize_symbol_variants(self) -> None:
        from quantdesk.shared import normalize_symbol

        self.assertEqual(normalize_symbol("002115"), "002115")
        self.assertEqual(normalize_symbol(2115), "002115")
        self.assertEqual(normalize_symbol(2115.0), "002115")
        self.assertEqual(normalize_symbol("sh600744"), "600744")
        self.assertEqual(normalize_symbol("sz002115"), "002115")
        self.assertEqual(normalize_symbol("600744.SH"), "600744")
        self.assertEqual(normalize_symbol("002115.SZ"), "002115")

    def test_truthy_variants(self) -> None:
        from quantdesk.shared import truthy

        for value in (True, 1, "1", "true", "True", "yes", "y", "是", "included=true"):
            self.assertTrue(truthy(value), value)
        for value in (False, 0, "0", "false", "False", "no", "n", "否", ""):
            self.assertFalse(truthy(value), value)

    def test_read_write_json_keeps_chinese(self) -> None:
        from quantdesk.shared import read_json, write_json

        path = self.tmp / "中文.json"
        write_json(path, {"name": "华银电力", "values": [1, 2, 3]})
        self.assertEqual(read_json(path)["name"], "华银电力")
        self.assertIn("华银电力", path.read_text(encoding="utf-8"))

    def test_shared_io_sanitizes_non_finite_numbers(self) -> None:
        from quantdesk.shared import read_json, write_json

        path = self.tmp / "safe_numbers.json"
        write_json(path, {"nan": float("nan"), "inf": float("inf"), "nested": [float("-inf"), 1.5]})

        text = path.read_text(encoding="utf-8")
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        self.assertEqual(read_json(path), {"nan": None, "inf": None, "nested": [None, 1.5]})

    def test_shared_json_write_retries_transient_replace_permission_error(self) -> None:
        from unittest.mock import patch

        import quantdesk.shared as shared

        path = self.tmp / "retry.json"
        original_replace = shared.os.replace
        attempts = {"count": 0}

        def flaky_replace(src: Path, dst: Path) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise PermissionError("transient replace lock")
            original_replace(src, dst)

        with patch("quantdesk.shared.os.replace", side_effect=flaky_replace):
            shared.write_json(path, {"ok": True})

        self.assertEqual(shared.read_json(path), {"ok": True})
        self.assertEqual(attempts["count"], 2)

    def test_shared_csv_and_zero_default_numeric_helpers(self) -> None:
        from quantdesk.shared import read_csv_rows, safe_float_zero, safe_int_zero, write_csv_rows

        path = self.tmp / "rows.csv"
        write_csv_rows(path, [{"name": "alpha", "value": 2}], fieldnames=["name", "value"])

        self.assertEqual(read_csv_rows(path), [{"name": "alpha", "value": "2"}])
        self.assertEqual(safe_float_zero("bad", 3.5), 3.5)
        self.assertEqual(safe_float_zero("inf", 2.0), 2.0)
        self.assertEqual(safe_int_zero("bad", 7), 7)

    def test_visible_text_mojibake_detector_remains_available(self) -> None:
        from quantdesk.self_check_core import contains_mojibake

        self.assertTrue(contains_mojibake("foo????bar"))
        self.assertFalse(contains_mojibake("欢迎使用本地研究工具。"))

    def test_candidate_row_preserves_strategy_type(self) -> None:
        from quantdesk.candidate_contract import normalize_candidate_row

        board = normalize_candidate_row({"symbol": "600744", "strategy_type": "board"}, default_strategy_type="long")
        self.assertEqual(board["strategy_type"], "board")
        self.assertEqual(board["strategy_label"], "打板")
        missing = normalize_candidate_row({"symbol": "600744"}, default_strategy_type="long")
        self.assertEqual(missing["strategy_type"], "long")

    def test_strategy_payload_rows_and_split(self) -> None:
        from quantdesk.candidate_contract import candidate_rows_from_strategy_payload, split_candidates_by_strategy

        payload = {
            "long_candidates": [{"symbol": "600744", "strategy_type": "long"}],
            "board_candidates": [{"symbol": "002115", "strategy_type": "board"}],
        }
        rows = candidate_rows_from_strategy_payload(payload)
        self.assertEqual([row["strategy_type"] for row in rows], ["long", "board"])
        split = split_candidates_by_strategy(rows)
        self.assertEqual(split["long"][0]["symbol"], "600744")
        self.assertEqual(split["board"][0]["symbol"], "002115")

    def test_attach_theme_map_matches_leading_zero(self) -> None:
        from quantdesk.candidate_contract import attach_theme_map_to_candidates, summarize_candidate_theme_coverage

        rows = [{"symbol": "2115", "strategy_type": "board"}]
        mapped = attach_theme_map_to_candidates(rows, {"002115": {"symbol": "002115", "sector": "通信", "industry": "通信设备", "primary_theme": "5G"}})
        self.assertEqual(mapped[0]["symbol"], "002115")
        self.assertEqual(mapped[0]["sector"], "通信")
        self.assertEqual(mapped[0]["primary_theme"], "5G")
        self.assertEqual(summarize_candidate_theme_coverage(mapped)["mapped_count"], 1)

    def test_attach_freshness_labels(self) -> None:
        from quantdesk.candidate_contract import attach_freshness_to_candidates

        rows = [{"symbol": "600744"}]
        fresh = attach_freshness_to_candidates(rows, {"target_complete_trade_date": "2026-05-15", "daily_feature_date": "2026-05-15", "is_daily_stale": False})
        stale = attach_freshness_to_candidates(rows, {"target_complete_trade_date": "2026-05-15", "daily_feature_date": "2026-05-14", "is_daily_stale": True})
        self.assertTrue(fresh[0]["is_target_date_covered"])
        self.assertEqual(fresh[0]["data_freshness_label"], "目标日期候选")
        self.assertFalse(stale[0]["is_target_date_covered"])
        self.assertEqual(stale[0]["data_freshness_label"], "滞后数据候选")

    def test_workflow_input_source_and_fallback_warning(self) -> None:
        import quantdesk.daily_workflow_orchestrator as workflow

        step = workflow._finish_step(workflow._start_step("auto_strategy_sandbox"), "ok", "ok", outputs={}, input_source="in_memory")
        warnings: list[str] = []
        workflow._latest_file_warning("auto_strategy_sandbox", warnings)
        self.assertEqual(step["input_source"], "in_memory")
        self.assertEqual(step["outputs"]["input_source"], "in_memory")
        self.assertIn("latest 文件", warnings[0])

    def test_auto_paper_portfolio_candidate_rows_keep_board(self) -> None:
        from quantdesk.candidate_contract import candidate_rows_from_strategy_payload

        rows = candidate_rows_from_strategy_payload({"board_candidates": [{"symbol": "600744", "strategy_type": "board"}]})
        self.assertEqual(rows[0]["strategy_type"], "board")
        self.assertEqual(rows[0]["strategy_label"], "打板")

    def test_conditional_plan_uses_candidate_contract_and_not_legacy(self) -> None:
        from quantdesk.paper.conditional_trade_plan import build_conditional_trade_plan

        daily_dir = self.tmp / "daily"
        daily_dir.mkdir()
        universe = self.tmp / "low_price_learning_symbols.csv"
        with universe.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["symbol", "name", "included"])
            writer.writeheader()
            writer.writerow({"symbol": "600744", "name": "华银电力", "included": "true"})
        with (daily_dir / "600744.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["date", "open", "high", "low", "close", "volume", "amount"])
            writer.writeheader()
            for idx in range(30):
                writer.writerow({"date": f"2026-04-{idx + 1:02d}" if idx < 29 else "2026-05-15", "open": 8, "high": 9, "low": 7, "close": 8.5, "volume": 1000, "amount": 10000})
        payload = build_conditional_trade_plan(
            auto_strategy_payload={"board_candidates": [{"symbol": "600744", "name": "华银电力", "strategy_type": "board", "price": 8.5}]},
            state={"positions": []},
            strategy="all",
            plan_date="2026-05-15",
            trade_date="2026-05-18",
            write=False,
            daily_dir=daily_dir,
            low_price_universe_path=universe,
            daily_feature_path=self.tmp / "missing_features.csv",
        )
        self.assertEqual(payload["symbols"][0]["strategy_type"], "board")
        self.assertEqual(payload["symbols"][0]["strategy_label"], "打板")
        source_text = (PROJECT_ROOT / "quantdesk" / "paper" / "conditional_trade_plan.py").read_text(encoding="utf-8", errors="ignore")
        self.assertNotIn("paper_trading_latest", source_text.lower())

    def test_active_desk_contract_copy_and_layout_guards(self) -> None:
        """Account naming stays explicit and long text cannot break the layout.

        Re-anchored from the retired index.html / app.js / styles.css. The active
        desk carries its CSS inline in the template, so the overflow guards are
        asserted there. The old 手动观察仓 / 旧版纸面组合 label does not exist on the
        active desk and is not asserted - inventing a substitute would let a real
        regression pass.
        """
        html = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(
            encoding="utf-8", errors="ignore"
        )
        bundle = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(
            encoding="utf-8", errors="ignore"
        )
        # the automatic virtual portfolio is named as such, never as a real account
        self.assertIn("自动虚拟组合", bundle)
        self.assertIn("strategy_label", bundle)
        # long text guards live in the template's inline CSS
        self.assertIn("overflow-wrap: anywhere", html)
        self.assertIn("word-break: break-word", html)


if __name__ == "__main__":
    unittest.main()

