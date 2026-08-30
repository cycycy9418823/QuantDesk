from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from quantdesk.risk.liquidity_warning import evaluate_liquidity_warning
from quantdesk.risk.portfolio_concentration import evaluate_concentration
from quantdesk.risk.risk_alerts import build_risk_alerts_report
from quantdesk.risk.smart_stop import (
    compute_atr,
    compute_sector_decay_stop,
    compute_time_stop,
    compute_trailing_stop,
)

try:
    from tests._operation_log_isolation import isolate_operation_log_dir
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _operation_log_isolation import isolate_operation_log_dir

isolate_operation_log_dir()

ROOT = Path(__file__).resolve().parents[1]


class V360SmartStopRiskAlertsTests(unittest.TestCase):
    def test_atr_calculation(self):
        atr = compute_atr([{"high": 11, "low": 9, "close": 10}, {"high": 12, "low": 10, "close": 11}], period=2)
        self.assertGreater(atr or 0, 0)

    def test_trailing_stop_payload(self):
        payload = compute_trailing_stop({"symbol": "600000", "current_price": 10}, [{"high": 10.5, "low": 9.5, "close": 10}])
        self.assertEqual(payload["stop_type"], "atr_trailing_stop")
        self.assertIn(payload["trigger_status"], {"safe", "watch", "triggered", "unknown"})

    def test_time_stop_payload(self):
        self.assertEqual(compute_time_stop({"symbol": "600000"}, "2026-05-26")["stop_type"], "time_stop")

    def test_sector_decay_stop_watch(self):
        payload = compute_sector_decay_stop({"symbol": "600000"}, {"sector_status": "weakening"})
        self.assertEqual(payload["trigger_status"], "watch")

    def test_concentration_warning(self):
        payload = evaluate_concentration([{"symbol": "A", "weight": 0.3, "sector": "S"}])
        self.assertGreaterEqual(payload["alert_count"], 1)

    def test_liquidity_warning(self):
        payload = evaluate_liquidity_warning({"liquidity_score": 10, "avg_amount_5d": 1000})
        self.assertIn(payload["severity"], {"low", "medium", "high"})

    def test_risk_alerts_synthetic(self):
        payload = build_risk_alerts_report({"synthetic_test_data": True})
        self.assertEqual(payload["task_type"], "risk_alerts")
        self.assertGreaterEqual(payload["alert_count"], 1)

    def test_cli_smart_stop_summary_json(self):
        proc = subprocess.run(
            [sys.executable, "scripts/qd.py", "smart-stop", "--synthetic-test-data", "true", "--json", "true", "--summary", "true"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(proc.stdout)["task_type"], "smart_stop")


if __name__ == "__main__":
    unittest.main()
