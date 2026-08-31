from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from quantdesk.chanlun_bi_engine import (
    DISCLAIMER,
    build_bi_from_fractals,
    normalize_fractals_for_bi,
    run_chanlun_bi_analysis,
    select_effective_fractals,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "chanlun" / "bi_sample.csv"


def fractal(ftype: str, index: int, high: float, low: float, *, confirmed: bool = True, symbol: str = "600744") -> dict:
    return {
        "symbol": symbol,
        "timeframe": "daily",
        "fractal_id": f"{symbol}_daily_{index}_{ftype}",
        "fractal_type": ftype,
        "date": f"2026-05-{index + 1:02d}",
        "datetime": None,
        "bar_index": index,
        "high": high,
        "low": low,
        "confirmed": confirmed,
    }


class ChanlunBiEngineTests(unittest.TestCase):
    def test_normalize_fractals_for_bi_reads_v302_payload(self):
        payload = {"symbol": "002115", "timeframe": "daily", "fractals": [fractal("bottom", 1, 9, 4, symbol="002115")]}
        result = normalize_fractals_for_bi(payload)
        self.assertEqual(result["fractals"][0]["symbol"], "002115")
        self.assertEqual(result["normalized_fractal_count"], 1)

    def test_consecutive_top_keeps_higher_top(self):
        rows = [fractal("top", 1, 10, 6), fractal("top", 2, 12, 7), fractal("bottom", 7, 9, 3)]
        selected = select_effective_fractals(rows)
        self.assertEqual(selected["effective_fractals"][0]["high"], 12)
        self.assertEqual(selected["diagnostics"]["removed_fractal_count"], 1)

    def test_consecutive_bottom_keeps_lower_bottom(self):
        rows = [fractal("bottom", 1, 9, 4), fractal("bottom", 2, 8, 3), fractal("top", 7, 12, 8)]
        selected = select_effective_fractals(rows)
        self.assertEqual(selected["effective_fractals"][0]["low"], 3)
        self.assertEqual(selected["diagnostics"]["removed_fractal_count"], 1)

    def test_alternating_fractals_form_bi(self):
        result = build_bi_from_fractals([fractal("bottom", 1, 9, 4), fractal("top", 6, 12, 8)], min_separation=4)
        self.assertEqual(result["diagnostics"]["bi_count"], 1)

    def test_bottom_top_forms_up_bi(self):
        result = build_bi_from_fractals([fractal("bottom", 1, 9, 4), fractal("top", 6, 12, 8)], min_separation=4)
        self.assertEqual(result["bi_list"][0]["direction"], "up")

    def test_top_bottom_forms_down_bi(self):
        result = build_bi_from_fractals([fractal("top", 1, 12, 8), fractal("bottom", 6, 9, 4)], min_separation=4)
        self.assertEqual(result["bi_list"][0]["direction"], "down")

    def test_top_top_cannot_directly_form_bi(self):
        result = build_bi_from_fractals([fractal("top", 1, 12, 8), fractal("top", 6, 13, 9)], min_separation=4)
        self.assertEqual(result["diagnostics"]["bi_count"], 0)

    def test_bottom_bottom_cannot_directly_form_bi(self):
        result = build_bi_from_fractals([fractal("bottom", 1, 9, 4), fractal("bottom", 6, 8, 3)], min_separation=4)
        self.assertEqual(result["diagnostics"]["bi_count"], 0)

    def test_min_separation_not_met_rejects_bi(self):
        result = build_bi_from_fractals([fractal("bottom", 1, 9, 4), fractal("top", 3, 12, 8)], min_separation=4)
        self.assertEqual(result["diagnostics"]["bi_count"], 0)
        self.assertEqual(result["rejected_bi"][0]["rejected_reason"], "min_separation_not_met")

    def test_min_separation_met_generates_bi(self):
        result = build_bi_from_fractals([fractal("bottom", 1, 9, 4), fractal("top", 5, 12, 8)], min_separation=4)
        self.assertEqual(result["diagnostics"]["bi_count"], 1)

    def test_unconfirmed_endpoint_not_output_by_default(self):
        result = build_bi_from_fractals([fractal("bottom", 1, 9, 4), fractal("top", 5, 12, 8, confirmed=False)], min_separation=4)
        self.assertEqual(result["diagnostics"]["bi_count"], 0)
        self.assertEqual(result["rejected_bi"][0]["rejected_reason"], "unconfirmed_fractal")

    def test_allow_provisional_outputs_provisional(self):
        result = build_bi_from_fractals([fractal("bottom", 1, 9, 4), fractal("top", 5, 12, 8, confirmed=False)], min_separation=4, allow_provisional=True)
        self.assertTrue(result["bi_list"][0]["provisional"])

    def test_up_bi_price_change_positive(self):
        result = build_bi_from_fractals([fractal("bottom", 1, 9, 4), fractal("top", 5, 12, 8)], min_separation=4)
        self.assertGreater(result["bi_list"][0]["price_change"], 0)

    def test_down_bi_price_change_negative(self):
        result = build_bi_from_fractals([fractal("top", 1, 12, 8), fractal("bottom", 5, 9, 4)], min_separation=4)
        self.assertLess(result["bi_list"][0]["price_change"], 0)

    def test_strength_contains_percent_change_and_slope(self):
        result = build_bi_from_fractals([fractal("bottom", 1, 9, 4), fractal("top", 5, 12, 8)], min_separation=4)
        strength = result["bi_list"][0]["strength"]
        self.assertIn("percent_change", strength)
        self.assertIn("slope", strength)

    def test_insufficient_data_does_not_crash(self):
        result = build_bi_from_fractals([fractal("bottom", 1, 9, 4)], min_separation=4)
        self.assertEqual(result["status"], "no_bi")

    def test_run_from_input_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chanlun_bi_analysis("600744", input_path=FIXTURE, output_dir=tmp, min_separation=4, write=True)
            self.assertTrue(result["ok"])
            self.assertGreaterEqual(result["diagnostics"]["bi_count"], 1)

    def test_run_from_fractal_path(self):
        payload = {
            "symbol": "600744",
            "timeframe": "daily",
            "processed_bars": [],
            "input_summary": {"raw_bar_count": 10, "processed_bar_count": 10, "start_date": "2026-05-01", "end_date": "2026-05-18"},
            "fractals": [fractal("bottom", 1, 9, 4), fractal("top", 6, 12, 8)],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fractals.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            result = run_chanlun_bi_analysis("600744", fractal_path=path, output_dir=tmp, min_separation=4, write=True)
            self.assertEqual(result["diagnostics"]["bi_count"], 1)

    def test_cli_runs_synthetic_input(self):
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_chanlun_bi_analysis.py"),
            "--input-path",
            str(FIXTURE),
            "--symbol",
            "600744",
            "--timeframe",
            "daily",
        ]
        completed = subprocess.run(cmd, cwd=PROJECT_ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("chanlun_bi_analysis", completed.stdout)


    def test_report_contains_learning_boundary(self):
        """Bi output is structure-learning only, never a signal to act on.

        Re-anchored from the retired app/index.html onto the generated report. The
        active 8000 desk ships no chanlun surface.
        """
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chanlun_bi_analysis(
                "600744", input_path=FIXTURE, output_dir=tmp, min_separation=4, write=True
            )
            report = Path(result["report_path"]).read_text(encoding="utf-8")
        self.assertIn("仅用于结构学习", report)

    def test_report_contains_disclaimer(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chanlun_bi_analysis("600744", input_path=FIXTURE, output_dir=tmp, min_separation=4, write=True)
            report_path = Path(result["report_path"])
            self.assertIn("不构成投资建议", report_path.read_text(encoding="utf-8"))
            self.assertIn(DISCLAIMER, result["disclaimer"])

    def test_generated_report_avoids_aggressive_phrases(self):
        """The bi report must never promise a return or push a trade."""
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chanlun_bi_analysis(
                "600744", input_path=FIXTURE, output_dir=tmp, min_separation=4, write=True
            )
            report = Path(result["report_path"]).read_text(encoding="utf-8")
        for phrase in ["推荐买入", "稳赚", "必涨"]:
            self.assertNotIn(phrase, report)

    def test_active_desk_avoids_aggressive_phrases(self):
        """Split from the report scan above.

        The old method scanned the retired index.html and app.js. Those are gone,
        but the phrase ban itself is a GENERAL boundary that applies to whatever
        the product renders - so it re-anchors onto the active template and bundle.
        This is not a claim that the active desk shows chanlun output; it asserts
        the desk never uses this language anywhere.
        """
        html = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(
            encoding="utf-8", errors="ignore"
        )
        bundle = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(
            encoding="utf-8", errors="ignore"
        )
        for phrase in ["推荐买入", "稳赚", "必涨"]:
            self.assertNotIn(phrase, html + bundle)


if __name__ == "__main__":
    unittest.main()


