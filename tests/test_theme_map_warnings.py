from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from quantdesk import theme_map_updater


class ThemeMapWarningsTests(unittest.TestCase):
    def test_akshare_fetch_failure_is_logged(self) -> None:
        fake = types.ModuleType("akshare")
        fake.stock_board_industry_name_em = lambda: (_ for _ in ()).throw(RuntimeError("provider boom"))
        fake.stock_board_concept_name_em = lambda: (_ for _ in ()).throw(RuntimeError("concept boom"))
        with patch.dict(sys.modules, {"akshare": fake}):
            with self.assertLogs("quantdesk.theme_map_updater", level="WARNING") as logs:
                self.assertEqual(theme_map_updater._try_akshare_industry(), {})
                self.assertEqual(theme_map_updater._try_akshare_concept(), {})
        combined = "\n".join(logs.output)
        self.assertIn("akshare industry name fetch failed", combined)
        self.assertIn("akshare concept name fetch failed", combined)


if __name__ == "__main__":
    unittest.main()
