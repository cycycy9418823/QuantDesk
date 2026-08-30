from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestV290DataFreshnessGateAndThemeSync(unittest.TestCase):
    def setUp(self) -> None:
        tmp_root = PROJECT_ROOT / ".tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.tmp_obj = tempfile.TemporaryDirectory(dir=tmp_root)
        self.tmp = Path(self.tmp_obj.name)

    def tearDown(self) -> None:
        self.tmp_obj.cleanup()

    def test_stale_daily_blocks_yesterday_review(self) -> None:
        from quantdesk.data_freshness_gate import evaluate_data_freshness

        gate = evaluate_data_freshness(
            daily_price_payload={
                "target_date": "2026-05-15",
                "latest_local_date": "2026-05-14",
                "max_local_date": "2026-05-14",
                "stale_count": 80,
                "missing_count": 0,
                "is_stale": True,
            },
            daily_feature_payload={"latest_date": "2026-05-14"},
            symbols=["600744"],
        )
        self.assertTrue(gate["is_daily_stale"])
        self.assertFalse(gate["can_review_yesterday_positions"])
        self.assertFalse(gate["portfolio_review_valid"])
        self.assertEqual(gate["review_mode"], "stale_observation")

    def test_waiting_review_does_not_emit_strategy_correctness(self) -> None:
        from quantdesk.data_freshness_gate import waiting_decision_review, waiting_yesterday_holding_review

        gate = {"target_complete_trade_date": "2026-05-15", "reason": "stale"}
        decision = waiting_decision_review([{"symbol": "600744", "action": "buy"}], gate)
        holding = waiting_yesterday_holding_review([{"symbol": "600744", "quantity": 100, "entry_price": 8.89}], gate)
        self.assertFalse(decision["review_valid"])
        self.assertEqual(decision["buy_correct_count"], 0)
        self.assertEqual(decision["buy_failed_count"], 0)
        self.assertFalse(holding["review_valid"])
        self.assertEqual(holding["items"][0]["review_conclusion"], "等待数据")
        self.assertIsNone(holding["items"][0]["today_price"])

    def test_complete_daily_without_intraday_allows_valuation_not_trigger_verification(self) -> None:
        from quantdesk.data_freshness_gate import evaluate_data_freshness

        gate = evaluate_data_freshness(
            daily_price_payload={
                "target_date": "2026-05-15",
                "latest_local_date": "2026-05-15",
                "max_local_date": "2026-05-15",
                "stale_count": 0,
                "missing_count": 0,
                "is_stale": False,
            },
            daily_feature_payload={"latest_date": "2026-05-15"},
            symbols=["600744"],
        )
        self.assertTrue(gate["can_update_portfolio_valuation"])
        self.assertFalse(gate["can_verify_intraday_triggers"])
        self.assertEqual(gate["review_mode"], "daily_review_only")

    def test_review_scope_is_not_blocked_by_unrelated_stale_pool_symbols(self) -> None:
        from quantdesk.data_freshness_gate import evaluate_data_freshness

        gate = evaluate_data_freshness(
            daily_price_payload={
                "target_date": "2026-07-10",
                "total_symbols": 3,
                "latest_local_date": "2026-07-08",
                "max_local_date": "2026-07-10",
                "latest_by_symbol": {
                    "600001": "2026-07-10",
                    "920001": "2026-07-08",
                    "920002": "2026-07-08",
                },
                "stale_count": 2,
                "missing_count": 0,
                "is_stale": True,
            },
            daily_feature_payload={"latest_date": "2026-07-10"},
            symbols=["600001"],
        )

        self.assertFalse(gate["is_daily_stale"])
        self.assertTrue(gate["can_review_yesterday_positions"])
        self.assertEqual(gate["local_daily_latest_date"], "2026-07-10")
        self.assertEqual(gate["review_scope_stale_symbol_count"], 0)
        self.assertEqual(gate["daily_pool_stale_symbol_count"], 2)
        self.assertTrue(gate["daily_pool_partial"])

    def test_review_scope_reads_local_date_for_symbols_missing_from_pool_map(self) -> None:
        from quantdesk.data_freshness_gate import evaluate_data_freshness

        with patch(
            "quantdesk.data_freshness_gate.latest_date_for_symbol",
            return_value="2026-07-10",
            create=True,
        ):
            gate = evaluate_data_freshness(
                daily_price_payload={
                    "target_date": "2026-07-10",
                    "total_symbols": 2,
                    "latest_local_date": "2026-07-08",
                    "max_local_date": "2026-07-10",
                    "latest_by_symbol": {"600001": "2026-07-10"},
                    "stale_count": 1,
                    "missing_count": 0,
                    "is_stale": True,
                },
                daily_feature_payload={"latest_date": "2026-07-10"},
                symbols=["600001", "600002"],
            )

        self.assertFalse(gate["is_daily_stale"])
        self.assertEqual(gate["review_scope_missing_symbol_count"], 0)
        self.assertEqual(gate["review_scope_latest_by_symbol"]["600002"], "2026-07-10")

    def test_current_candidate_theme_mapping_preserves_leading_zero(self) -> None:
        import quantdesk.theme_current_candidate_validator as validator

        strategy_path = self.tmp / "auto_strategy_latest.json"
        latest_path = self.tmp / "auto_paper_portfolio_latest.json"
        state_path = self.tmp / "auto_paper_portfolio.json"
        theme_path = self.tmp / "stock_theme_map.csv"
        strategy_path.write_text(json.dumps({"long_candidates": [{"symbol": "2115", "name": "三维通信"}]}, ensure_ascii=False), encoding="utf-8")
        latest_path.write_text("{}", encoding="utf-8")
        state_path.write_text("{}", encoding="utf-8")
        theme_path.write_text(
            "symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at\n"
            "002115,三维通信,通信,通信设备,通信;5G,通信;5G,通信5G,seed,2026-05-16\n",
            encoding="utf-8",
        )
        with patch.object(validator, "AUTO_STRATEGY_LATEST_JSON", strategy_path), patch.object(
            validator, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", latest_path
        ), patch.object(validator, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_path):
            payload = validator.validate_theme_map_for_current_candidates(theme_path)
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["mapped_current_candidates"], 1)
        self.assertEqual(payload["missing_symbols"], [])

    def test_pending_theme_rows_are_distinguished_from_absent(self) -> None:
        # A candidate whose symbol IS present in the theme map but only as a
        # pending placeholder (未知板块/source=pending) must be reported as
        # "pending" (mapping row exists, classification not yet filled), NOT as
        # "absent"/"symbol mismatch". This keeps the degraded copy honest: the
        # symbol is known, the sector data is simply awaiting a (network) refresh.
        import quantdesk.theme_current_candidate_validator as validator

        strategy_path = self.tmp / "auto_strategy_latest.json"
        latest_path = self.tmp / "auto_paper_portfolio_latest.json"
        state_path = self.tmp / "auto_paper_portfolio.json"
        theme_path = self.tmp / "stock_theme_map.csv"
        strategy_path.write_text(
            json.dumps({"long_candidates": [{"symbol": "600744", "name": "华银电力"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        latest_path.write_text("{}", encoding="utf-8")
        state_path.write_text("{}", encoding="utf-8")
        theme_path.write_text(
            "symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at\n"
            "600744,华银电力,未知板块,未知行业,未知概念,未知题材,未知题材,pending,2026-07-01\n",
            encoding="utf-8",
        )
        with patch.object(validator, "AUTO_STRATEGY_LATEST_JSON", strategy_path), patch.object(
            validator, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", latest_path
        ), patch.object(validator, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_path):
            payload = validator.validate_theme_map_for_current_candidates(theme_path)
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["mapped_current_candidates"], 0)
        # The symbol is present in the map (pending), so it must be surfaced as
        # pending, and NOT listed as an absent/missing symbol.
        self.assertEqual(payload["pending_symbols"], ["600744"])
        self.assertEqual(payload["absent_symbols"], [])
        self.assertEqual(payload["pending_current_candidates"], 1)
        self.assertIn("待补", payload["reason"])

    def test_pending_theme_row_uses_local_paper_position_sector_fallback(self) -> None:
        import quantdesk.theme_current_candidate_validator as validator

        strategy_path = self.tmp / "auto_strategy_latest.json"
        latest_path = self.tmp / "auto_paper_portfolio_latest.json"
        state_path = self.tmp / "auto_paper_portfolio.json"
        theme_path = self.tmp / "stock_theme_map.csv"
        strategy_path.write_text("{}", encoding="utf-8")
        latest_path.write_text(
            json.dumps(
                {
                    "positions": [
                        {
                            "symbol": "600744",
                            "name": "华银电力",
                            "sector": "公用事业",
                            "industry": "电力",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state_path.write_text("{}", encoding="utf-8")
        theme_path.write_text(
            "symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at\n"
            "600744,华银电力,未知板块,未知行业,未知概念,未知题材,未知题材,pending,2026-07-01\n",
            encoding="utf-8",
        )
        with patch.object(validator, "AUTO_STRATEGY_LATEST_JSON", strategy_path), patch.object(
            validator, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", latest_path
        ), patch.object(validator, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_path):
            payload = validator.validate_theme_map_for_current_candidates(theme_path)
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["mapped_current_candidates"], 1)
        self.assertEqual(payload["pending_symbols"], [])
        self.assertEqual(payload["missing_symbols"], [])

    def test_ui_contains_stale_and_current_theme_mapping_copy(self) -> None:
        app = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
        html = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("当前候选板块映射", app)
        self.assertIn("滞后数据观察", app)
        self.assertIn("暂无有效复盘", app)
        self.assertIn("完整日线控制收益复盘，分钟线控制盘中触发复盘", html)

    def test_report_boundary_copy_is_present(self) -> None:
        workflow = (PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("不接券商", workflow)
        self.assertIn("不真实下单", workflow)
        self.assertIn("不构成投资建议", workflow)


if __name__ == "__main__":
    unittest.main()
