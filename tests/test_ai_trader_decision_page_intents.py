from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_trader.decision_log import render_owner_page  # noqa: E402

INTENT = {
    "as_of": "2026-09-16",
    "at": "2026-09-16T10:30:00",
    "symbol": "600000",
    "name": "浦发银行",
    "side": "buy",
    "precondition": "preset_touch_price",
    "level": 9.01,
    "observed_price": 9.1,
    "observed_low": 9.0,
    "observed_high": 9.35,
    "status": "intent_only",
    "filled": False,
    "note": "当日最低 9.0、现价 9.1 已触及计划买点 9.01，仅记意图，不按此价成交。",
}


class DecisionPageIntentSectionTests(unittest.TestCase):
    def _render(self, intents):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        html_path = root / "decision_page.html"
        md_path = root / "decision_page.md"
        render_owner_page([], html_path=html_path, md_path=md_path, intents=intents)
        return tmp, md_path.read_text(encoding="utf-8"), html_path.read_text(encoding="utf-8")

    def test_intent_section_rendered_when_present(self):
        tmp, md, html = self._render([INTENT])
        try:
            self.assertIn("白天盘中意图（未成交，仅记录）", md)
            self.assertIn("600000", md)
            self.assertIn("preset_touch_price", md)
            self.assertIn("没有成交", html)
            self.assertIn("600000", html)
        finally:
            tmp.cleanup()

    def test_no_section_when_absent(self):
        tmp, md, html = self._render(None)
        try:
            self.assertNotIn("白天盘中意图", md)
            self.assertNotIn("白天盘中意图", html)
        finally:
            tmp.cleanup()

    def test_empty_list_also_renders_no_section(self):
        tmp, md, html = self._render([])
        try:
            self.assertNotIn("白天盘中意图", md)
            self.assertNotIn("白天盘中意图", html)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
