"""Offline deterministic tests for the Market Mainline Radar (research-only).

All fixtures are in-memory; no network, no LLM, no filesystem writes to
data/daily.  Covers: normal data, missing theme mapping, empty candidate pool,
empty holdings, missing/degraded data, and the no-investment-advice boundary.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
import unittest

from quantdesk import market_mainline_radar as radar


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _inputs(**overrides):
    base = dict(
        as_of="2026-06-29",
        target_date="2026-06-29",
        data_cutoff="2026-06-29",
        spot_rows=[
            {"symbol": "000001", "pct_chg": 9.9, "amount": 5.0e8},
            {"symbol": "000002", "pct_chg": 5.2, "amount": 3.0e8},
            {"symbol": "000003", "pct_chg": 1.1, "amount": 2.0e8},
            {"symbol": "600000", "pct_chg": -3.0, "amount": 1.0e8},
            {"symbol": "600036", "pct_chg": -1.5, "amount": 1.2e8},
        ],
        theme_map={
            "000001": {"sector": "半导体", "primary_theme": "国产替代"},
            "000002": {"sector": "半导体", "primary_theme": "国产替代"},
            "000003": {"sector": "半导体", "primary_theme": "国产替代"},
            "600000": {"sector": "银行", "primary_theme": "银行"},
            "600036": {"sector": "银行", "primary_theme": "银行"},
        },
        industry_board=[],
        candidate_symbols={"000001", "000002"},
        watchlist_symbols={"000003"},
        holding_symbols={"000001"},
        benchmark_pct=0.5,
    )
    base.update(overrides)
    return radar.RadarInputs(**base)


def _readiness(mainline: str = "partial") -> dict:
    return {
        "status": "partial",
        "capabilities": {
            "mainline_analysis": mainline,
            "moneyflow_context": "partial",
            "event_context": "partial",
            "fundamental_context": "partial",
        },
        "domains": {
            "sector_index": {"status": "staged_not_imported"},
            "sector_membership": {"status": "partial"},
            "concept_theme": {"status": "partial"},
            "moneyflow": {"status": "staged_not_imported"},
            "events": {"status": "staged_not_imported"},
            "fundamentals": {"status": "partial"},
        },
        "warnings": ["staged_not_imported:sector_index"],
    }


# Affirmative advice phrasing that must never appear. We deliberately match
# *recommendation* forms (建议买入/目标价为/...) rather than bare words, because
# the boundary disclaimers legitimately contain negated forms such as
# "不构成投资建议、目标价或仓位建议" and "不构成仓位或买卖建议".
_FORBIDDEN = [
    "建议买入", "建议卖出", "建议加仓", "建议减仓", "建议满仓", "建议清仓",
    "目标价为", "目标价：", "目标价:", "止损价为", "必然上涨", "必涨",
    "buy now", "sell now", "should buy", "should sell",
]


def _all_text(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


class MarketMainlineRadarTests(unittest.TestCase):
    def test_missing_date_contract_blocks_all_current_point_in_time_outputs(self) -> None:
        payload = radar.build_mainline_radar(
            _inputs(as_of="", target_date="", data_cutoff="")
        )

        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["evidence_eligible"])
        self.assertEqual(payload["data_quality"]["status"], "blocked")
        self.assertIn("mainline_missing_as_of", payload["reason_codes"])
        self.assertIn("mainline_missing_target_date", payload["reason_codes"])
        self.assertIn("mainline_missing_data_cutoff", payload["reason_codes"])
        for key in (
            "current_mainlines",
            "secondary_lines",
            "potential_lines",
            "cooling_lines",
            "sector_scores",
        ):
            self.assertEqual(payload[key], [], key)

    def test_target_date_mismatch_cannot_remain_current_mainline(self) -> None:
        payload = radar.build_mainline_radar(
            _inputs(
                as_of="2026-07-10",
                target_date="2026-06-29",
                data_cutoff="2026-06-29",
            )
        )

        self.assertFalse(payload["evidence_eligible"])
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["ok"])
        for key in (
            "current_mainlines",
            "secondary_lines",
            "potential_lines",
            "cooling_lines",
            "sector_scores",
        ):
            self.assertEqual(payload[key], [], key)
        self.assertTrue(payload["historical_mainlines"])
        self.assertIn("mainline_as_of_target_mismatch", payload["reason_codes"])

    def test_stale_cutoff_is_blocked_instead_of_treated_as_current(self) -> None:
        payload = radar.build_mainline_radar(
            _inputs(
                as_of="2026-06-29",
                target_date="2026-06-29",
                data_cutoff="2026-06-28",
            )
        )

        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["evidence_eligible"])
        self.assertIn("mainline_stale_data_cutoff", payload["reason_codes"])
        self.assertEqual(payload["sector_scores"], [])

    def test_real_loader_injects_explicit_target_and_spot_data_cutoff(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "cache").mkdir(parents=True)
            (root / "data" / "universe").mkdir(parents=True)
            (root / "data" / "cache" / "spot_snapshot_latest.csv").write_text(
                "symbol,pct_chg,amount,quote_time\n"
                "000001,6.0,100000000,2026-07-03 15:00:00\n",
                encoding="utf-8",
            )
            (root / "data" / "universe" / "stock_theme_map.csv").write_text(
                "symbol,sector,primary_theme\n"
                "000001,semiconductor,domestic_substitution\n",
                encoding="utf-8",
            )

            inputs = radar.load_radar_inputs(
                project_root=root,
                target_date="2026-07-04",
            )

        self.assertEqual(inputs.as_of, "2026-07-04")
        self.assertEqual(inputs.target_date, "2026-07-04")
        self.assertEqual(inputs.data_cutoff, "2026-07-03")

    def test_real_payload_blocks_stale_spot_snapshot_and_clears_formal_outputs(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "cache").mkdir(parents=True)
            (root / "data" / "universe").mkdir(parents=True)
            (root / "data" / "cache" / "spot_snapshot_latest.csv").write_text(
                "symbol,pct_chg,amount,quote_time\n"
                "000001,9.9,500000000,2026-07-03 15:00:00\n"
                "000002,5.2,300000000,2026-07-03 15:00:00\n"
                "000003,1.1,200000000,2026-07-03 15:00:00\n",
                encoding="utf-8",
            )
            (root / "data" / "universe" / "stock_theme_map.csv").write_text(
                "symbol,sector,primary_theme\n"
                "000001,semiconductor,domestic_substitution\n"
                "000002,semiconductor,domestic_substitution\n"
                "000003,semiconductor,domestic_substitution\n",
                encoding="utf-8",
            )

            payload = radar.build_mainline_radar_payload(
                project_root=root,
                target_date="2026-07-04",
            )

        self.assertEqual(payload["as_of"], "2026-07-04")
        self.assertEqual(payload["target_date"], "2026-07-04")
        self.assertEqual(payload["data_cutoff"], "2026-07-03")
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["evidence_eligible"])
        self.assertIn("mainline_stale_data_cutoff", payload["reason_codes"])
        for key in (
            "current_mainlines",
            "secondary_lines",
            "potential_lines",
            "cooling_lines",
            "sector_scores",
        ):
            self.assertEqual(payload[key], [], key)

    def test_normal_data_outputs_mainlines_and_scores(self) -> None:
        payload = radar.build_mainline_radar(_inputs())
        self.assertEqual(payload["scope"], "research_only")
        self.assertEqual(payload["source"], "local_research")
        self.assertEqual(payload["data_quality"]["status"], "ok")
        self.assertTrue(payload["sector_scores"], "expected non-empty sector_scores")
        names = {item["name"] for item in payload["current_mainlines"]}
        self.assertIn("半导体", names)
        # Every mainline item carries evidence + confirmation/failure signals.
        for item in payload["current_mainlines"]:
            self.assertTrue(item["evidence"])
            self.assertTrue(item["confirmation_signals"])
            self.assertTrue(item["failure_signals"])
            self.assertTrue(item["not_advice"])

    def test_missing_theme_mapping_degrades(self) -> None:
        # No theme map at all -> members cannot be grouped -> degraded/missing.
        payload = radar.build_mainline_radar(_inputs(theme_map={}))
        self.assertIn(payload["data_quality"]["status"], {"degraded", "missing", "partial"})
        self.assertTrue(payload["data_quality"]["warnings"], "expected a degradation warning")

    def test_partial_theme_mapping_warns(self) -> None:
        # Only one symbol mapped -> low coverage ratio -> partial/degraded warning.
        payload = radar.build_mainline_radar(
            _inputs(theme_map={"000001": {"sector": "半导体", "primary_theme": "国产替代"}})
        )
        self.assertIn(payload["data_quality"]["status"], {"partial", "degraded"})
        self.assertTrue(payload["data_quality"]["warnings"])

    def test_empty_candidate_pool_is_safe(self) -> None:
        payload = radar.build_mainline_radar(_inputs(candidate_symbols=set()))
        self.assertEqual(payload["candidate_overlap"], [])
        # Still produces sector scores from price/amount data.
        self.assertTrue(payload["sector_scores"])

    def test_empty_holdings_is_safe(self) -> None:
        payload = radar.build_mainline_radar(_inputs(holding_symbols=set()))
        self.assertEqual(payload["holding_overlap"], [])
        self.assertTrue(payload["sector_scores"])

    def test_missing_spot_data_is_missing_not_fabricated(self) -> None:
        payload = radar.build_mainline_radar(_inputs(spot_rows=[]))
        self.assertIn(payload["data_quality"]["status"], {"missing", "degraded"})
        self.assertTrue(payload["data_quality"]["warnings"])
        # No fabricated mainlines when there is no market data.
        self.assertEqual(payload["current_mainlines"], [])
        self.assertEqual(payload["sector_scores"], [])

    def test_no_investment_advice_language(self) -> None:
        payload = radar.build_mainline_radar(_inputs())
        text = _all_text(payload)
        # Only affirmative advice phrasing is forbidden. Disclaimer wording like
        # "不构成投资建议、目标价或仓位建议" legitimately mentions these terms in
        # a *negated* boundary statement and must be preserved, so we check for
        # affirmative recommendation phrasing instead of bare keywords.
        for phrase in _FORBIDDEN:
            self.assertNotIn(phrase, text, f"advice-like phrase leaked: {phrase}")
        # Boundary markers must be present.
        self.assertTrue(payload.get("not_advice"))
        self.assertIn("disclaimer", payload)

    def test_overlap_notes_never_become_position_advice(self) -> None:
        payload = radar.build_mainline_radar(_inputs())
        for item in payload["candidate_overlap"] + payload["holding_overlap"]:
            hint = item.get("stage_hint", "")
            # The hint must NOT phrase overlap as an affirmative buy/sell/position
            # action, but a negated disclaimer ("不构成仓位或买卖建议") is required.
            for phrase in _FORBIDDEN:
                self.assertNotIn(phrase, hint, f"advice-like phrase leaked in stage_hint: {phrase}")
            self.assertIn("不构成", hint)

    def test_research_prompt_is_offline_text_only(self) -> None:
        payload = radar.build_mainline_radar(_inputs())
        prompt = radar.build_mainline_research_prompt(payload)
        self.assertIsInstance(prompt, str)
        self.assertTrue(prompt.strip())
        # The prompt builder must not itself call a model — it only returns text.
        # Boundary wording should be embedded so any downstream summary stays safe.
        self.assertIn("Research Only", prompt)

    def test_extended_data_readiness_degrades_quality_without_changing_scores(self) -> None:
        normal = radar.build_mainline_radar(_inputs())
        payload = radar.build_mainline_radar(_inputs(extended_data_readiness=_readiness(mainline="partial")))

        self.assertEqual(payload["data_quality"]["status"], "partial")
        self.assertEqual(payload["extended_data_readiness"]["mainline_analysis"], "partial")
        self.assertEqual(payload["extended_data_readiness"]["domain_statuses"]["sector_index"], "staged_not_imported")
        self.assertIn("extended_mainline_data_partial", payload["data_quality"]["warnings"])
        self.assertEqual(payload["sector_scores"][0]["score"], normal["sector_scores"][0]["score"])

    def test_mainline_radar_cli_writes_json_and_markdown(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "cache").mkdir(parents=True)
            (root / "data" / "universe").mkdir(parents=True)
            (root / "data" / "cache" / "spot_snapshot_latest.csv").write_text(
                "symbol,pct_chg,amount,quote_time\n"
                "000001,6.0,100000000,2026-07-03 15:00:00\n"
                "000002,3.0,80000000,2026-07-03 15:00:00\n"
                "600000,-1.0,50000000,2026-07-03 15:00:00\n",
                encoding="utf-8",
            )
            (root / "data" / "universe" / "stock_theme_map.csv").write_text(
                "symbol,sector,primary_theme\n"
                "000001,semiconductor,domestic_substitution\n"
                "000002,semiconductor,domestic_substitution\n"
                "600000,bank,bank\n",
                encoding="utf-8",
            )
            out = root / "mainline.json"
            report = root / "mainline.md"
            completed = subprocess.run(
                [
                    PYTHON,
                    str(PROJECT_ROOT / "scripts" / "run_market_mainline_radar.py"),
                    "--project-root",
                    str(root),
                    "--output",
                    str(out),
                    "--report",
                    str(report),
                    "--target-date",
                    "2026-07-03",
                    "--json",
                    "true",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["task_type"], "market_mainline_radar")
            self.assertTrue(payload["evidence_eligible"])
            self.assertTrue(out.exists())
            self.assertTrue(report.exists())
            self.assertIn("Market Mainline Radar", report.read_text(encoding="utf-8"))

            help_text = subprocess.check_output(
                [PYTHON, str(PROJECT_ROOT / "scripts" / "qd.py"), "help"],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertIn("mainline-radar", help_text)


if __name__ == "__main__":
    unittest.main()
