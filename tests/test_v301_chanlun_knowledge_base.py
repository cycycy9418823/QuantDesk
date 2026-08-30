from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk import chanlun_knowledge_base as kb


class ChanlunKnowledgeBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        (PROJECT_ROOT / ".tmp").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".tmp")
        self.tmp_path = Path(self.tmp.name)
        self.report_dir = self.tmp_path / "reports"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build_payload(self) -> dict:
        with mock.patch.object(kb, "REPORT_DIR", self.report_dir):
            return kb.build_chanlun_knowledge_base(
                source_pdf=self.tmp_path / "missing_chanlun_108.pdf",
                output_dir=self.tmp_path / "knowledge",
                report=True,
                no_fulltext=True,
                write=True,
            )

    def test_generates_lessons_index_json(self) -> None:
        payload = self.build_payload()
        self.assertTrue(Path(payload["paths"]["lessons_index"]).exists())

    def test_generates_concepts_json(self) -> None:
        payload = self.build_payload()
        self.assertTrue(Path(payload["paths"]["concepts"]).exists())

    def test_generates_rule_roadmap_json(self) -> None:
        payload = self.build_payload()
        self.assertTrue(Path(payload["paths"]["rule_roadmap"]).exists())

    def test_lessons_index_has_108_lessons(self) -> None:
        payload = self.build_payload()
        self.assertEqual(payload["lesson_count"], 108)
        lessons = json.loads(Path(payload["paths"]["lessons_index"]).read_text(encoding="utf-8"))["lessons"]
        self.assertEqual(len(lessons), 108)

    def test_lessons_index_contains_fractal_related_lesson(self) -> None:
        lessons = self.build_payload()["lessons_index"]
        joined = json.dumps(lessons, ensure_ascii=False)
        self.assertIn("分型", joined)

    def test_lessons_index_contains_bi_related_lesson(self) -> None:
        lessons = self.build_payload()["lessons_index"]
        joined = json.dumps(lessons, ensure_ascii=False)
        self.assertIn("笔", joined)

    def test_lessons_index_contains_segment_related_lesson(self) -> None:
        lessons = self.build_payload()["lessons_index"]
        joined = json.dumps(lessons, ensure_ascii=False)
        self.assertIn("线段", joined)

    def test_lessons_index_contains_zhongshu_related_lesson(self) -> None:
        lessons = self.build_payload()["lessons_index"]
        joined = json.dumps(lessons, ensure_ascii=False)
        self.assertIn("中枢", joined)

    def test_lessons_index_contains_divergence_related_lesson(self) -> None:
        lessons = self.build_payload()["lessons_index"]
        joined = json.dumps(lessons, ensure_ascii=False)
        self.assertIn("背驰", joined)

    def test_concepts_include_fractal(self) -> None:
        names = {row["concept"] for row in self.build_payload()["concepts"]}
        self.assertIn("分型", names)

    def test_concepts_include_bi(self) -> None:
        names = {row["concept"] for row in self.build_payload()["concepts"]}
        self.assertIn("笔", names)

    def test_concepts_include_segment(self) -> None:
        names = {row["concept"] for row in self.build_payload()["concepts"]}
        self.assertIn("线段", names)

    def test_concepts_include_zhongshu(self) -> None:
        names = {row["concept"] for row in self.build_payload()["concepts"]}
        self.assertIn("中枢定义", names)

    def test_concepts_include_divergence(self) -> None:
        names = {row["concept"] for row in self.build_payload()["concepts"]}
        self.assertIn("趋势背驰", names)

    def test_buy_points_are_observation_signals(self) -> None:
        concepts = self.build_payload()["concepts"]
        points = [row for row in concepts if row["concept"] in {"第一类买点", "第二类买点", "第三类买点"}]
        self.assertEqual(len(points), 3)
        self.assertTrue(all(row["category"] == "观察信号" for row in points))
        self.assertTrue(all("观察信号" in row["risk_note"] for row in points))

    def test_does_not_save_pdf_fulltext(self) -> None:
        payload = self.build_payload()
        text = "\n".join(Path(payload["paths"][key]).read_text(encoding="utf-8") for key in ("lessons_index", "concepts", "rule_roadmap"))
        self.assertNotIn("full_text", text)
        self.assertNotIn("raw_text", text)
        self.assertNotIn("原文段落", text)

    def test_report_contains_disclaimer(self) -> None:
        payload = self.build_payload()
        report = Path(payload["paths"]["report"]).read_text(encoding="utf-8")
        self.assertIn("不构成投资建议", report)

    def test_report_avoids_aggressive_phrases(self) -> None:
        payload = self.build_payload()
        report = Path(payload["paths"]["report"]).read_text(encoding="utf-8")
        for phrase in ("推荐买入", "稳赚", "必涨"):
            self.assertNotIn(phrase, report)

    def test_ui_contains_chanlun_learning_tab(self) -> None:
        html = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        self.assertIn("缠论学习", html)

    def test_ui_states_buy_sell_points_are_observation_signals(self) -> None:
        html = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        self.assertIn("买卖点仅作为观察信号", html)

    def test_clean_zip_excludes_sources(self) -> None:
        ps1 = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8")
        self.assertIn("data\\knowledge\\sources", ps1)

    def test_get_status_route_is_read_only(self) -> None:
        server = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/chanlun/knowledge/status"', server)
        self.assertIn("chanlun_knowledge_status_response()", server)

    def test_post_build_route_generates_knowledge_base(self) -> None:
        server = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/chanlun/knowledge/build"', server)
        self.assertIn("chanlun_knowledge_build_response(payload)", server)


if __name__ == "__main__":
    unittest.main()



