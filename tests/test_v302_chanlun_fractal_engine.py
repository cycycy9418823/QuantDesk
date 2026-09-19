from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from tests._isolated_project import IsolatedProject
import quantdesk.chanlun_fractal_engine as engine
from pathlib import Path

import pandas as pd

from quantdesk.chanlun_fractal_engine import (
    detect_fractals,
    normalize_chanlun_ohlc,
    resolve_include_relationship,
    run_chanlun_fractal_analysis,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "chanlun" / "fractal_sample.csv"


def bars(rows: list[tuple[str, float, float, float, float]]) -> list[dict]:
    df = pd.DataFrame(
        [
            {"symbol": "600744", "date": date, "open": open_, "high": high, "low": low, "close": close}
            for date, open_, high, low, close in rows
        ]
    )
    return normalize_chanlun_ohlc(df, symbol="600744")["bars"]


class ChanlunFractalEngineTests(unittest.TestCase):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        paths = mock.patch.multiple(
            engine, PROJECT_ROOT=self.runtime, REPORTS_DIR=self.runtime / "reports",
            CHANLUN_FRACTAL_RUNTIME_DIR=self.runtime / "data" / "chanlun",
        )
        paths.start()
        self.addCleanup(paths.stop)

    def test_normalize_english_ohlc(self) -> None:
        df = pd.DataFrame([{"symbol": "600744", "date": "2026-05-15", "open": 8, "high": 9, "low": 7, "close": 8.5}])
        out = normalize_chanlun_ohlc(df, timeframe="daily")
        self.assertTrue(out["ok"])
        self.assertEqual(out["bars"][0]["high"], 9.0)

    def test_normalize_chinese_fields(self) -> None:
        df = pd.DataFrame([{"代码": "600744", "日期": "2026-05-15", "开盘": 8, "最高": 9, "最低": 7, "收盘": 8.5}])
        out = normalize_chanlun_ohlc(df, timeframe="daily")
        self.assertTrue(out["ok"])
        self.assertEqual(out["bars"][0]["symbol"], "600744")

    def test_symbol_leading_zero_kept(self) -> None:
        df = pd.DataFrame([{"代码": "002115", "日期": "2026-05-15", "开盘": 8, "最高": 9, "最低": 7, "收盘": 8.5}])
        out = normalize_chanlun_ohlc(df, timeframe="daily")
        self.assertEqual(out["bars"][0]["symbol"], "002115")

    def test_dates_are_sorted(self) -> None:
        df = pd.DataFrame([
            {"symbol": "600744", "date": "2026-05-16", "open": 8, "high": 9, "low": 7, "close": 8.5},
            {"symbol": "600744", "date": "2026-05-15", "open": 7, "high": 8, "low": 6, "close": 7.5},
        ])
        out = normalize_chanlun_ohlc(df)
        self.assertEqual([row["date"] for row in out["bars"]], ["2026-05-15", "2026-05-16"])

    def test_invalid_ohlc_returns_warning_or_invalid(self) -> None:
        df = pd.DataFrame([{"symbol": "600744", "date": "2026-05-15", "open": 8, "high": 7, "low": 9, "close": 8.5}])
        out = normalize_chanlun_ohlc(df)
        self.assertFalse(out["ok"])
        self.assertIn(out["status"], {"invalid_ohlc", "invalid_schema"})
        self.assertTrue(out["warnings"] or out["errors"])

    def test_include_relationship_a_contains_b(self) -> None:
        sample = bars([("2026-05-15", 7, 10, 5, 7.5), ("2026-05-16", 8, 9, 6, 8.5)])
        out = resolve_include_relationship(sample, direction_mode="up")
        self.assertEqual(out["merged_count"], 1)
        self.assertEqual(out["bars"][0]["source_count"], 2)

    def test_include_relationship_b_contains_a(self) -> None:
        sample = bars([("2026-05-15", 8, 9, 6, 8.5), ("2026-05-16", 7, 10, 5, 7.5)])
        out = resolve_include_relationship(sample, direction_mode="down")
        self.assertEqual(out["merged_count"], 1)
        self.assertEqual(out["bars"][0]["source_count"], 2)

    def test_up_include_merge_rule(self) -> None:
        sample = bars([("2026-05-15", 7, 10, 5, 7.5), ("2026-05-16", 8, 9, 6, 8.5)])
        merged = resolve_include_relationship(sample, direction_mode="up")["bars"][0]
        self.assertEqual(merged["high"], 10.0)
        self.assertEqual(merged["low"], 6.0)

    def test_down_include_merge_rule(self) -> None:
        sample = bars([("2026-05-15", 8, 9, 6, 8.5), ("2026-05-16", 7, 10, 5, 7.5)])
        merged = resolve_include_relationship(sample, direction_mode="down")["bars"][0]
        self.assertEqual(merged["high"], 9.0)
        self.assertEqual(merged["low"], 5.0)

    def test_top_fractal_detected(self) -> None:
        sample = bars([("2026-05-11", 7, 10, 5, 7.5), ("2026-05-12", 10, 12, 7, 11), ("2026-05-13", 8, 11, 6, 8.5)])
        out = detect_fractals(sample, include_resolved=False, mode="strict", symbol="600744")
        self.assertEqual(out["fractals"][0]["fractal_type"], "top")

    def test_bottom_fractal_detected(self) -> None:
        sample = bars([("2026-05-13", 8, 11, 6, 8.5), ("2026-05-14", 5, 9, 4, 5.5), ("2026-05-15", 8, 10, 5, 9)])
        out = detect_fractals(sample, include_resolved=False, mode="strict", symbol="600744")
        self.assertEqual(out["fractals"][0]["fractal_type"], "bottom")

    def test_last_bar_is_not_confirmed_fractal(self) -> None:
        sample = bars([("2026-05-11", 7, 10, 5, 7.5), ("2026-05-12", 10, 12, 7, 11)])
        out = detect_fractals(sample, include_resolved=False, mode="strict", symbol="600744")
        self.assertEqual(out["fractals"], [])

    def test_strict_more_conservative_than_loose(self) -> None:
        sample = bars([("2026-05-11", 7, 10, 5, 7.5), ("2026-05-12", 10, 12, 4, 11), ("2026-05-13", 8, 11, 6, 8.5)])
        strict = detect_fractals(sample, include_resolved=False, mode="strict", symbol="600744")
        loose = detect_fractals(sample, include_resolved=False, mode="loose", symbol="600744")
        self.assertLessEqual(len(strict["fractals"]), len(loose["fractals"]))
        self.assertEqual(loose["fractals"][0]["reliability"], "low")

    def test_fractal_output_contains_confirmed(self) -> None:
        sample = bars([("2026-05-11", 7, 10, 5, 7.5), ("2026-05-12", 10, 12, 7, 11), ("2026-05-13", 8, 11, 6, 8.5)])
        row = detect_fractals(sample, include_resolved=False, symbol="600744")["fractals"][0]
        self.assertTrue(row["confirmed"])

    def test_fractal_output_contains_confirmation_bar_index(self) -> None:
        sample = bars([("2026-05-11", 7, 10, 5, 7.5), ("2026-05-12", 10, 12, 7, 11), ("2026-05-13", 8, 11, 6, 8.5)])
        row = detect_fractals(sample, include_resolved=False, symbol="600744")["fractals"][0]
        self.assertEqual(row["confirmation_bar_index"], 2)

    def test_fractal_output_contains_disclaimer(self) -> None:
        sample = bars([("2026-05-11", 7, 10, 5, 7.5), ("2026-05-12", 10, 12, 7, 11), ("2026-05-13", 8, 11, 6, 8.5)])
        row = detect_fractals(sample, include_resolved=False, symbol="600744")["fractals"][0]
        self.assertIn("不构成投资建议", row["disclaimer"])

    def test_run_no_data_does_not_crash(self) -> None:
        out = run_chanlun_fractal_analysis("600744", input_path=PROJECT_ROOT / ".tmp" / "missing_chanlun.csv", write=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "no_data")

    def test_run_skips_bad_csv_rows_with_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chanlun_bad_rows.csv"
            path.write_text(
                "\n".join(
                    [
                        "symbol,date,open,high,low,close",
                        "600744,2026-05-11,7,10,5,7.5",
                        "bad,row,with,too,many,columns,for,this,csv",
                        "600744,2026-05-12,10,12,7,11",
                        "600744,2026-05-13,8,11,6,8.5",
                    ]
                ),
                encoding="utf-8-sig",
            )

            out = run_chanlun_fractal_analysis("600744", input_path=path, write=False)

            self.assertTrue(out["ok"])
            self.assertEqual(out["input_summary"]["raw_bar_count"], 3)
            self.assertTrue(any("csv_bad_lines_skipped" in item for item in out["diagnostics"]["warnings"]))

    def test_cli_runs_synthetic_input(self) -> None:
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_chanlun_fractal_analysis.py"),
            "--input-path",
            str(FIXTURE),
            "--symbol",
            "600744",
            "--timeframe",
            "daily",
        ]
        with IsolatedProject(PROJECT_ROOT, temp_parent=self.runtime) as project:
            cmd[1] = str(project.root / "scripts" / "run_chanlun_fractal_analysis.py")
            result = subprocess.run(cmd, cwd=project.root, env=project.subprocess_env(), text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("chanlun_fractal_analysis", result.stdout)


    def test_result_carries_learning_boundary(self) -> None:
        """Fractal output is structure-learning only, never a signal to act on.

        Re-anchored from the retired app/index.html. The active 8000 desk ships no
        chanlun surface, so the boundary is asserted where a consumer actually
        receives it: the result payload's disclaimer field. Verified that the
        markdown report carries only 不构成投资建议, so asserting the full phrase
        against the report would be asserting something the engine never writes
        there.
        """
        out = run_chanlun_fractal_analysis("600744", input_path=FIXTURE, write=False, report=False)
        self.assertIn("仅用于结构学习", out["disclaimer"])
        self.assertIn("不构成投资建议", out["disclaimer"])

    def test_report_contains_disclaimer(self) -> None:
        out = run_chanlun_fractal_analysis("600744", input_path=FIXTURE, write=True, report=True)
        report = Path(out["report_path"]).read_text(encoding="utf-8")
        self.assertIn("不构成投资建议", report)

    def test_generated_report_avoids_aggressive_phrases(self) -> None:
        """The fractal report must never promise a return or push a trade."""
        out = run_chanlun_fractal_analysis("600744", input_path=FIXTURE, write=True, report=True)
        report = Path(out["report_path"]).read_text(encoding="utf-8")
        for phrase in ("推荐买入", "稳赚", "必涨"):
            self.assertNotIn(phrase, report)

    def test_active_desk_avoids_aggressive_phrases(self) -> None:
        """Split from the report scan above; twin of the bi/segment engine cases.

        The old method also scanned the retired app/index.html. That file is gone,
        but the phrase ban is a GENERAL product boundary, so it re-anchors onto the
        active template and bundle. Not a claim that the active desk renders
        chanlun output.
        """
        html = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(
            encoding="utf-8", errors="ignore"
        )
        bundle = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(
            encoding="utf-8", errors="ignore"
        )
        for phrase in ("推荐买入", "稳赚", "必涨"):
            self.assertNotIn(phrase, html + bundle)


if __name__ == "__main__":
    unittest.main()


