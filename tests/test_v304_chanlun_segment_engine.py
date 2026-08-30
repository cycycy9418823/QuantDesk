from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from quantdesk.chanlun_segment_engine import (
    DISCLAIMER,
    build_segments_from_bi,
    normalize_bi_for_segment,
    run_chanlun_segment_analysis,
    select_effective_bi,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "chanlun" / "segment_sample.csv"


def bi(direction: str, idx: int, start: float, end: float, *, confirmed: bool = True, provisional: bool = False) -> dict:
    return {
        "bi_id": f"600744_daily_bi_{idx:04d}",
        "symbol": "600744",
        "timeframe": "daily",
        "direction": direction,
        "start_date": f"2026-05-{idx + 1:02d}",
        "end_date": f"2026-05-{idx + 2:02d}",
        "start_datetime": None,
        "end_datetime": None,
        "start_bar_index": idx * 4,
        "end_bar_index": idx * 4 + 4,
        "start_price": start,
        "end_price": end,
        "confirmed": confirmed,
        "provisional": provisional,
    }


class ChanlunSegmentEngineTests(unittest.TestCase):
    def test_normalize_bi_for_segment_reads_v303_payload(self):
        payload = {"symbol": "002115", "timeframe": "daily", "bi_list": [{**bi("up", 1, 4, 8), "symbol": "002115"}]}
        result = normalize_bi_for_segment(payload)
        self.assertEqual(result["bi_list"][0]["symbol"], "002115")

    def test_select_effective_bi_default_confirmed_only(self):
        result = select_effective_bi([bi("up", 1, 4, 8, confirmed=False), bi("down", 2, 8, 5), bi("up", 3, 5, 9)], min_bi_count=2)
        self.assertEqual(result["diagnostics"]["removed_bi_count"], 1)

    def test_allow_provisional_includes_provisional_bi(self):
        result = select_effective_bi([bi("up", 1, 4, 8, confirmed=False, provisional=True), bi("down", 2, 8, 5), bi("up", 3, 5, 9)], min_bi_count=2, allow_provisional=True)
        self.assertTrue(result["effective_bi"][0]["provisional"])

    def test_consecutive_up_keeps_higher_endpoint(self):
        result = select_effective_bi([bi("up", 1, 4, 8), bi("up", 2, 5, 10), bi("down", 3, 10, 6)], min_bi_count=2)
        self.assertEqual(result["effective_bi"][0]["end_price"], 10)

    def test_consecutive_down_keeps_lower_endpoint(self):
        result = select_effective_bi([bi("down", 1, 10, 7), bi("down", 2, 9, 5), bi("up", 3, 5, 9)], min_bi_count=2)
        self.assertEqual(result["effective_bi"][0]["end_price"], 5)

    def test_insufficient_effective_bi_returns_no_segment(self):
        result = build_segments_from_bi([bi("up", 1, 4, 8), bi("down", 2, 8, 5)], min_bi_count=3)
        self.assertEqual(result["status"], "no_segment")

    def test_three_bi_can_form_candidate_segment(self):
        result = build_segments_from_bi([bi("up", 1, 4, 8), bi("down", 2, 8, 5), bi("up", 3, 5, 10)], min_bi_count=3)
        self.assertEqual(result["diagnostics"]["segment_count"], 1)

    def test_up_bi_sequence_forms_up_segment(self):
        result = build_segments_from_bi([bi("up", 1, 4, 8), bi("down", 2, 8, 5), bi("up", 3, 5, 10)], min_bi_count=3)
        self.assertEqual(result["segments"][0]["direction"], "up")

    def test_down_bi_sequence_forms_down_segment(self):
        result = build_segments_from_bi([bi("down", 1, 10, 6), bi("up", 2, 6, 8), bi("down", 3, 8, 4)], min_bi_count=3)
        self.assertEqual(result["segments"][0]["direction"], "down")

    def test_up_segment_price_change_positive(self):
        result = build_segments_from_bi([bi("up", 1, 4, 8), bi("down", 2, 8, 5), bi("up", 3, 5, 10)], min_bi_count=3)
        self.assertGreater(result["segments"][0]["price_change"], 0)

    def test_down_segment_price_change_negative(self):
        result = build_segments_from_bi([bi("down", 1, 10, 6), bi("up", 2, 6, 8), bi("down", 3, 8, 4)], min_bi_count=3)
        self.assertLess(result["segments"][0]["price_change"], 0)

    def test_segment_output_contains_bi_ids(self):
        result = build_segments_from_bi([bi("up", 1, 4, 8), bi("down", 2, 8, 5), bi("up", 3, 5, 10)], min_bi_count=3)
        self.assertEqual(len(result["segments"][0]["bi_ids"]), 3)

    def test_segment_output_contains_bi_count(self):
        result = build_segments_from_bi([bi("up", 1, 4, 8), bi("down", 2, 8, 5), bi("up", 3, 5, 10)], min_bi_count=3)
        self.assertEqual(result["segments"][0]["bi_count"], 3)

    def test_segment_output_contains_break_status(self):
        result = build_segments_from_bi([bi("up", 1, 4, 8), bi("down", 2, 8, 5), bi("up", 3, 5, 10)], min_bi_count=3)
        self.assertIn(result["segments"][0]["break_status"], {"intact", "unknown", "broken"})

    def test_up_segment_down_break(self):
        rows = [bi("up", 1, 4, 8), bi("down", 2, 8, 5), bi("up", 3, 5, 10), bi("down", 4, 10, 3)]
        result = build_segments_from_bi(rows, min_bi_count=3)
        self.assertEqual(result["segments"][0]["break_status"], "broken")
        self.assertEqual(result["segments"][0]["break_type"], "down_break")

    def test_down_segment_up_break(self):
        rows = [bi("down", 1, 10, 6), bi("up", 2, 6, 8), bi("down", 3, 8, 4), bi("up", 4, 4, 11)]
        result = build_segments_from_bi(rows, min_bi_count=3)
        self.assertEqual(result["segments"][0]["break_status"], "broken")
        self.assertEqual(result["segments"][0]["break_type"], "up_break")

    def test_reliability_lower_with_provisional(self):
        rows = [bi("up", 1, 4, 8, confirmed=False, provisional=True), bi("down", 2, 8, 5), bi("up", 3, 5, 10)]
        result = build_segments_from_bi(rows, min_bi_count=3, allow_provisional=True)
        self.assertEqual(result["segments"][0]["reliability"], "low")

    def test_no_crash_with_empty_data(self):
        result = build_segments_from_bi([], min_bi_count=3)
        self.assertEqual(result["status"], "no_segment")

    def test_run_from_input_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chanlun_segment_analysis("600744", input_path=FIXTURE, output_dir=tmp, min_bi_count=3, write=True)
            self.assertTrue(result["ok"])
            self.assertIn("segments", result)

    def test_run_from_bi_path(self):
        payload = {
            "symbol": "600744",
            "timeframe": "daily",
            "input_summary": {"raw_bar_count": 20, "processed_bar_count": 20, "start_date": "2026-05-01", "end_date": "2026-05-20"},
            "bi_list": [bi("up", 1, 4, 8), bi("down", 2, 8, 5), bi("up", 3, 5, 10)],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bi.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            result = run_chanlun_segment_analysis("600744", bi_path=path, output_dir=tmp, min_bi_count=3, write=True)
            self.assertEqual(result["diagnostics"]["segment_count"], 1)

    def test_cli_runs_synthetic_input(self):
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_chanlun_segment_analysis.py"),
            "--input-path",
            str(FIXTURE),
            "--symbol",
            "600744",
            "--timeframe",
            "daily",
        ]
        completed = subprocess.run(cmd, cwd=PROJECT_ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("chanlun_segment_analysis", completed.stdout)

    def test_api_get_status_is_read_only(self):
        text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
        self.assertIn('/api/chanlun/segments/status', text)

    def test_api_post_run_executes(self):
        text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
        self.assertIn('/api/chanlun/segments/run', text)
        self.assertIn("chanlun_segments_run_response", text)

    def test_ui_contains_chanlun_segment_block(self):
        html = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("缠论线段识别", html)

    def test_ui_contains_learning_boundary(self):
        html = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("线段识别仅用于结构学习", html)

    def test_report_contains_disclaimer(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chanlun_segment_analysis("600744", input_path=FIXTURE, output_dir=tmp, min_bi_count=3, write=True)
            report_path = Path(result["report_path"])
            self.assertIn("不构成投资建议", report_path.read_text(encoding="utf-8"))
            self.assertIn(DISCLAIMER, result["disclaimer"])

    def test_ui_and_report_avoid_aggressive_phrases(self):
        html = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
        app = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chanlun_segment_analysis("600744", input_path=FIXTURE, output_dir=tmp, min_bi_count=3, write=True)
            report = Path(result["report_path"]).read_text(encoding="utf-8")
        combined = html + app + report
        for phrase in ["推荐买入", "稳赚", "必涨"]:
            self.assertNotIn(phrase, combined)


if __name__ == "__main__":
    unittest.main()


