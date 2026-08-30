from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ResearchLoopContractTests(unittest.TestCase):
    def test_current_analysis_pool_prefers_screen_candidate_handoff(self) -> None:
        from quantdesk import scope_policy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "results" / "screen" / "screen_latest.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "candidate_handoff": {
                            "symbols": ["600001", "000002", "300003", "600001"],
                            "source": "screen_latest",
                            "target": "backtest/current_analysis_pool",
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = scope_policy.resolve_scope(scope="current_analysis_pool", max_symbols=2, project_root=root)

        self.assertEqual(payload["scope_source"], "screen_candidate_handoff")
        self.assertEqual(payload["analysis_pool_count"], 3)
        self.assertEqual(payload["selected_count"], 2)
        self.assertEqual(payload["symbols"], ["600001", "000002"])
        self.assertTrue(payload["truncated"])

    def test_screen_writes_candidate_handoff_for_backtest_scope(self) -> None:
        screen_module = importlib.import_module("scripts.screen_stocks")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            features = root / "features.csv"
            factor_health = root / "factor_health_latest.json"
            json_output = root / "screen_latest.json"
            report = root / "screen_report.md"
            features.write_text(
                "symbol,name,close,score\n"
                "600001,Alpha,10,88\n"
                "000002,Beta,-1,10\n",
                encoding="utf-8",
            )
            factor_health.write_text(
                json.dumps(
                    {
                        "task_type": "factor_evaluation",
                        "status": "passed",
                        "generated_at": "2026-06-23T10:00:00",
                        "data_source": "local_daily",
                        "factor_metrics": {
                            "momentum_20d": {
                                "rank_ic_mean": 0.06,
                                "ic_mean": 0.04,
                                "health_status": "usable",
                            }
                        },
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )

            code = screen_module.main(
                [
                    "--data",
                    str(features),
                    "--conditions",
                    "close > 0",
                    "--factor-health-json",
                    str(factor_health),
                    "--limit",
                    "5",
                    "--json-output",
                    str(json_output),
                    "--output",
                    str(report),
                ]
            )
            payload = json.loads(json_output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        handoff = payload.get("candidate_handoff") or {}
        self.assertEqual(handoff.get("source"), "screen_latest")
        self.assertEqual(handoff.get("target"), "backtest/current_analysis_pool")
        self.assertEqual(handoff.get("candidate_count"), 1)
        self.assertEqual(handoff.get("symbols"), ["600001"])
        self.assertTrue(handoff.get("research_only"))
        self.assertTrue(handoff.get("paper_only"))
        self.assertTrue(handoff.get("no_broker"))
        self.assertTrue(handoff.get("no_real_orders"))
        factor_context = payload.get("factor_health_context") or {}
        self.assertTrue(factor_context.get("available"))
        self.assertFalse(factor_context.get("used_for_scoring"))
        self.assertTrue(factor_context.get("evidence_only"))
        self.assertEqual((factor_context.get("summary") or {}).get("usable_count"), 1)

    def test_default_screen_excludes_rows_outside_target_complete_trade_date(self) -> None:
        screen_module = importlib.import_module("scripts.screen_stocks")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            features = root / "features.csv"
            json_output = root / "screen_latest.json"
            report = root / "screen_report.md"
            features.write_text(
                "symbol,name,latest_date,close,score\n"
                "600001,Fresh,2026-07-10,10,88\n"
                "920001,Stale,2026-07-08,12,99\n",
                encoding="utf-8",
            )

            code = screen_module.main(
                [
                    "--data",
                    str(features),
                    "--target-date",
                    "2026-07-10",
                    "--limit",
                    "5",
                    "--json-output",
                    str(json_output),
                    "--output",
                    str(report),
                ]
            )
            payload = json.loads(json_output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual([row.get("symbol") for row in payload.get("matched") or []], ["600001"])
        self.assertEqual((payload.get("candidate_handoff") or {}).get("symbols"), ["600001"])
        self.assertEqual(payload.get("target_date"), "2026-07-10")
        self.assertTrue(payload.get("freshness_enforced"))
        self.assertEqual(payload.get("stale_excluded_count"), 1)

    def test_canonical_screen_filter_fails_closed_when_feature_dates_are_missing(self) -> None:
        screen_module = importlib.import_module("scripts.screen_stocks")

        rows, freshness = screen_module._filter_current_feature_rows(
            [{"symbol": "600001", "close": 10.0}],
            target_date="2026-07-10",
            require_dates=True,
        )

        self.assertEqual(rows, [])
        self.assertTrue(freshness["freshness_enforced"])
        self.assertEqual(freshness["undated_excluded_count"], 1)

    def test_screen_reports_no_fresh_rows_instead_of_missing_features(self) -> None:
        screen_module = importlib.import_module("scripts.screen_stocks")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            features = root / "features.csv"
            json_output = root / "screen_latest.json"
            report = root / "screen_report.md"
            features.write_text(
                "symbol,name,latest_date,close\n"
                "920001,Stale,2026-07-08,12\n",
                encoding="utf-8",
            )

            code = screen_module.main(
                [
                    "--data",
                    str(features),
                    "--target-date",
                    "2026-07-10",
                    "--json-output",
                    str(json_output),
                    "--output",
                    str(report),
                ]
            )
            payload = json.loads(json_output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(payload.get("warning_kind"), "no_fresh_features_for_target")
        self.assertEqual(payload.get("target_date"), "2026-07-10")
        self.assertEqual(payload.get("stale_excluded_count"), 1)
        self.assertNotEqual(payload.get("warning_kind"), "missing_features")

    def test_screen_uses_real_combined_score_for_handoff_order(self) -> None:
        screen_module = importlib.import_module("scripts.screen_stocks")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            features = root / "features.csv"
            combined_score = root / "combined_score_latest.json"
            json_output = root / "screen_latest.json"
            report = root / "screen_report.md"
            features.write_text(
                "symbol,name,close,score\n"
                "600001,Alpha,10,88\n"
                "000002,Beta,10,10\n",
                encoding="utf-8",
            )
            combined_score.write_text(
                json.dumps(
                    {
                        "task_type": "factor_combination",
                        "status": "passed",
                        "data_source": "local_panel",
                        "target_date": "2026-06-23",
                        "top_candidates": [
                            {
                                "date": "2026-06-23",
                                "symbol": "000002",
                                "combined_score": 2.0,
                                "combined_score_rank": 1,
                                "coverage": 1.0,
                            },
                            {
                                "date": "2026-06-23",
                                "symbol": "600001",
                                "combined_score": 1.0,
                                "combined_score_rank": 2,
                                "coverage": 1.0,
                            },
                        ],
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )

            code = screen_module.main(
                [
                    "--data",
                    str(features),
                    "--conditions",
                    "close > 0",
                    "--combined-score-json",
                    str(combined_score),
                    "--limit",
                    "5",
                    "--json-output",
                    str(json_output),
                    "--output",
                    str(report),
                ]
            )
            payload = json.loads(json_output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual([row.get("symbol") for row in payload.get("matched") or []], ["000002", "600001"])
        self.assertEqual((payload.get("candidate_handoff") or {}).get("symbols"), ["000002", "600001"])
        context = payload.get("combined_score_context") or {}
        self.assertTrue(context.get("available"))
        self.assertTrue(context.get("used_for_sorting"))
        self.assertFalse(context.get("evidence_only"))
        self.assertEqual(context.get("matched_score_count"), 2)
        self.assertEqual((payload.get("matched") or [{}])[0].get("combined_score_rank"), 1)

    def test_agent_context_includes_latest_backtest_and_factor_evidence(self) -> None:
        api_server = importlib.import_module("scripts.quant_api_server")

        def fake_read_json_file(path: Path) -> dict[str, object]:
            text = str(path).replace("\\", "/")
            if text.endswith("results/backtest/backtest_latest.json"):
                return {
                    "status": "passed",
                    "strategy": "ma_breakout",
                    "scope_source": "screen_candidate_handoff",
                    "data_scope": "current_analysis_pool",
                    "symbols": ["600001", "000002"],
                    "trade_count": 3,
                    "generated_at": "2026-06-23T10:00:00",
                    "metrics": {
                        "total_return": 0.12,
                        "annual_return": 0.08,
                        "max_drawdown": -0.05,
                        "sharpe": 1.1,
                        "win_rate": 0.55,
                        "final_equity": 112000,
                    },
                    "warnings": ["chart_disabled"],
                }
            if text.endswith("results/factor_health_latest.json"):
                return {
                    "status": "passed",
                    "data_source": "local_daily",
                    "generated_at": "2026-06-23T09:00:00",
                    "factor_metrics": {
                        "momentum_20d": {
                            "rank_ic_mean": 0.06,
                            "ic_mean": 0.04,
                            "health_status": "usable",
                        }
                    },
                    "warnings": [],
                }
            if text.endswith("results/strategy_lab/v420_strategy_leaderboard_latest.json"):
                return {
                    "status": "completed_with_warnings",
                    "items": [
                        {
                            "strategy_id": "long_term_family",
                            "research_rank": 1,
                            "strategy_health": "research_healthy",
                            "research_readiness": "research_ready_with_warnings",
                            "robustness_score": 0.76,
                            "mapping_coverage": 0.92,
                            "annualized_return": 0.48,
                            "Sharpe": 1.31,
                            "max_drawdown": -0.31,
                            "win_rate": 0.50,
                            "data_risk_level": "MODERATE_HIGH",
                            "limitation_note": "research-only leaderboard; not a trade instruction",
                        }
                    ],
                    "research_only": True,
                    "no_broker": True,
                    "no_buy_sell_advice": True,
                }
            if text.endswith("results/strategy_lab/v420_real_use_strategy_lab_handoff_latest.json"):
                return {
                    "status": "v420_real_use_strategy_lab_completed_with_warnings",
                    "created_at": "2026-06-23T11:00:00",
                    "research_only_boundary_kept": True,
                    "remaining_gap_for_one_stop_research": "provider freshness still needs explicit network update",
                }
            if text.endswith("results/strategy_lab/v421_strategy_statistics_latest.json"):
                return {
                    "status": "completed",
                    "items": [
                        {
                            "strategy_family": "long_term_family",
                            "candidate_count": 10,
                            "signal_count": 8,
                            "limitation_note": "research-only statistics",
                        }
                    ],
                }
            return {}

        context: dict[str, object] = {}
        with mock.patch.object(api_server, "read_json_file", side_effect=fake_read_json_file):
            api_server._attach_latest_research_evidence_context(context)

        backtest = context["backtest_summary"]
        factor = context["factor_health"]
        strategy_lab = context["strategy_lab"]
        self.assertEqual(backtest["scope_source"], "screen_candidate_handoff")
        self.assertEqual(backtest["strategy"], "ma_breakout")
        self.assertEqual(backtest["symbols_preview"], ["600001", "000002"])
        self.assertTrue(backtest["research_only"])
        self.assertTrue(backtest["paper_only"])
        self.assertFalse(factor["used_for_scoring"])
        self.assertTrue(factor["evidence_only"])
        self.assertEqual(factor["summary"]["usable_count"], 1)
        self.assertTrue(strategy_lab["available"])
        self.assertFalse(strategy_lab["used_for_scoring"])
        self.assertTrue(strategy_lab["evidence_only"])
        self.assertTrue(strategy_lab["paper_only"])
        self.assertEqual(strategy_lab["top_strategies"][0]["strategy_id"], "long_term_family")
        self.assertEqual(strategy_lab["top_strategies"][0]["candidate_count"], 10)
        self.assertEqual(strategy_lab["top_strategies"][0]["mapping_coverage"], 0.92)
        self.assertTrue(any("provider freshness" in item for item in strategy_lab["warnings_top"]))


if __name__ == "__main__":
    unittest.main()
