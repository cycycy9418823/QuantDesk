from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from quantdesk.research.strategy_viability_data_precheck import build_strategy_viability_precheck
from quantdesk.research.strategy_viability_report import (
    build_strategy_viability_report,
    write_strategy_viability_report,
)
from quantdesk.research.strategy_viability_runner import run_strategy_viability_baselines
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

ROOT = Path(__file__).resolve().parents[1]


def _write_daily(path: Path, symbol: str, days: int = 180, *, drift: float = 0.001) -> None:
    dates = pd.bdate_range("2025-01-01", periods=days)
    rows = []
    price = 10.0 + (int(symbol[-2:]) * 0.01)
    for idx, date in enumerate(dates):
        price *= 1.0 + drift + ((idx % 7) - 3) * 0.0005
        rows.append({
            "date": date.strftime("%Y-%m-%d"),
            "symbol": symbol,
            "open": price * 0.99,
            "high": price * 1.01,
            "low": price * 0.98,
            "close": price,
            "volume": 100000 + idx,
            "amount": price * (100000 + idx),
            "pct_chg": "",
            "change": "",
            "turnover": 1.0,
        })
    pd.DataFrame(rows).to_csv(path / f"{symbol}.csv", index=False)


class StrategyViabilityV3701Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.daily = self.root / "data" / "daily"
        self.daily.mkdir(parents=True)
        for idx in range(25):
            _write_daily(self.daily, f"600{idx:03d}", drift=0.0005 + idx * 0.00002)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_001_version(self) -> None:
        # Expected: v3.7.0.1 strategy viability remains wired after patch releases.
        self.assertIn(PACKAGE_VERSION, {"3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(APP_VERSION, {"3.7.0.1-strategy-viability-on-data-backbone", "3.7.0.2-strategy-pipeline-repair", "3.7.0.3-low-price-pipeline-finalization", "3.7.0.4-capital-lot-sensitivity-and-git-hygiene", "3.7.0.5-git-deletion-reconciliation", "3.7.0.6-strategy-regression-guard-and-report-hygiene"})
        self.assertIn(API_SCHEMA_VERSION, {"3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})

    def test_002_precheck_marks_survivorship_high_without_history(self) -> None:
        # Expected: missing historical listings never reports low survivorship risk.
        payload = build_strategy_viability_precheck({"data_root": str(self.root / "data")})
        self.assertEqual(payload["survivorship_bias_risk"], "high")
        self.assertTrue(payload["partial_daily"]["isolated"])
        self.assertGreaterEqual(payload["total_csv_count"], 25)

    def test_003_precheck_schema_counts(self) -> None:
        # Expected: schema distribution and missing benchmark warnings are explicit.
        payload = build_strategy_viability_precheck({"data_root": str(self.root / "data")})
        self.assertGreaterEqual(payload["schema_variant_count"], 1)
        self.assertIn("benchmark_index_csv_missing", payload["warnings"])

    def test_004_runner_outputs_five_baselines(self) -> None:
        # Expected: the original five requested strategies remain reported, including weak ones.
        payload = run_strategy_viability_baselines({"data_root": str(self.root / "data"), "max_symbols": 25})
        self.assertGreaterEqual(payload["baseline_count"], 5)
        self.assertTrue({"momentum_20d", "low_volatility", "momentum_low_vol", "trend_continuation", "rsi_recovery"}.issubset({row["name"] for row in payload["baselines"]}))

    def test_005_runner_metrics_include_costs_and_years(self) -> None:
        # Expected: gross/net/cost metrics and yearly performance are present.
        payload = run_strategy_viability_baselines({"data_root": str(self.root / "data"), "max_symbols": 25})
        first = payload["baselines"][0]
        metrics = first["metrics"]
        self.assertIn("annual_return_gross", metrics)
        self.assertIn("annual_return_net", metrics)
        self.assertIn("trade_cost_drag", metrics)
        self.assertIsInstance(first["yearly_returns"], dict)

    def test_006_decision_gate_present(self) -> None:
        # Expected: report gives one of the bounded decision outcomes.
        payload = run_strategy_viability_baselines({"data_root": str(self.root / "data"), "max_symbols": 25})
        self.assertIn(payload["decision_gate"]["recommendation"], {"continue_research", "stop_and_rethink", "refine_strategy"})

    def test_007_report_payload(self) -> None:
        # Expected: final report combines data precheck and baseline results.
        baseline = run_strategy_viability_baselines({"data_root": str(self.root / "data"), "max_symbols": 25})
        self.assertGreaterEqual(baseline["baseline_count"], 5)
        payload = write_strategy_viability_report(build_strategy_viability_report({"data_root": str(self.root / "data"), "max_symbols": 25, "refresh": True}))
        self.assertEqual(payload["task_type"], "strategy_viability_report")
        self.assertIn("v3.7.1-strategy-refinement", payload["next_round_suggestions"])

    def test_008_qd_precheck_json(self) -> None:
        # Expected: qd strategy-viability precheck emits a single valid JSON summary.
        out = subprocess.check_output([sys.executable, "scripts/qd.py", "strategy-viability", "precheck", "--data-root", str(self.root / "data"), "--json", "true", "--summary", "true"], cwd=ROOT, text=True)
        payload = json.loads(out)
        self.assertEqual(payload["task_type"], "strategy_viability_precheck")

    def test_009_qd_run_json(self) -> None:
        # Expected: qd strategy-viability run emits baseline summary JSON.
        out = subprocess.check_output([sys.executable, "scripts/qd.py", "strategy-viability", "run", "--data-root", str(self.root / "data"), "--max-symbols", "25", "--json", "true", "--summary", "true"], cwd=ROOT, text=True)
        payload = json.loads(out)
        self.assertEqual(payload["task_type"], "strategy_viability_baselines")
        self.assertGreaterEqual(payload["baseline_count"], 5)

    def test_010_qd_report_json(self) -> None:
        # Expected: qd strategy-viability report emits final summary JSON.
        out = subprocess.check_output([sys.executable, "scripts/qd.py", "strategy-viability", "report", "--data-root", str(self.root / "data"), "--max-symbols", "25", "--refresh", "true", "--json", "true", "--summary", "true"], cwd=ROOT, text=True)
        payload = json.loads(out)
        self.assertEqual(payload["task_type"], "strategy_viability_report")
        self.assertIn(payload["recommendation"], {"continue_research", "stop_and_rethink", "refine_strategy"})


if __name__ == "__main__":
    unittest.main()


