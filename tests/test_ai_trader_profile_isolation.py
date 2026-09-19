from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_trader import paths as paths_mod  # noqa: E402
from ai_trader.ledger import empty_ledger, load_ledger  # noqa: E402
from ai_trader.paths import (  # noqa: E402
    CyclePaths,
    configured_profiles,
    ledger_id_for_path,
    ledger_id_for_profile,
    profile_paths,
    profile_state_dir,
)
from scripts.init_ai_trader_profile import fork_profile  # noqa: E402


class ProfileIsolationTests(unittest.TestCase):
    def test_default_profile_is_unchanged(self):
        self.assertEqual(profile_paths(""), CyclePaths())
        self.assertEqual(profile_paths(), CyclePaths())
        self.assertEqual(ledger_id_for_profile(), "ai_trader_100k")
        self.assertEqual(ledger_id_for_path(CyclePaths().ledger), "ai_trader_100k")

    def test_glm_profile_is_isolated_but_shares_market_data(self):
        glm = profile_paths("glm")
        default = CyclePaths()
        self.assertNotEqual(glm.ledger, default.ledger)
        self.assertEqual(glm.ledger.parent.name, "ai_trader_glm")
        self.assertEqual(glm.decision_page_md.parent.name, "ai_trader_glm")
        self.assertEqual(glm.daily_dir, default.daily_dir)
        self.assertEqual(glm.minute_dir, default.minute_dir)
        self.assertEqual(ledger_id_for_profile("glm"), "ai_trader_100k_glm")
        self.assertEqual(ledger_id_for_path(glm.ledger), "ai_trader_100k_glm")

    def test_ledger_identity_follows_its_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            glm_dir = Path(tmp) / "ai_trader_glm"
            glm_dir.mkdir()
            foreign = glm_dir / "ledger.json"
            foreign.write_text(json.dumps(empty_ledger(ledger_id="ai_trader_100k")), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_ledger(foreign)

            own = empty_ledger(ledger_id="ai_trader_100k_glm")
            foreign.write_text(json.dumps(own), encoding="utf-8")
            self.assertEqual(load_ledger(foreign).get("ledger_id"), "ai_trader_100k_glm")

    def test_configured_profiles_starts_with_default(self):
        self.assertEqual(configured_profiles()[0], "")


class ForkProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._orig_root = paths_mod.PROJECT_ROOT
        paths_mod.PROJECT_ROOT = self.tmp

    def tearDown(self):
        paths_mod.PROJECT_ROOT = self._orig_root
        self._tmp.cleanup()

    def _seed_source(self) -> Path:
        source = profile_state_dir("kimi")
        source.mkdir(parents=True)
        (source / "ledger.json").write_text(
            json.dumps(empty_ledger(ledger_id="ai_trader_100k_kimi")), encoding="utf-8"
        )
        (source / "plan_latest.json").write_text(json.dumps({"plan_id": "plan_1"}), encoding="utf-8")
        (source / "llm.env").write_text('export AI_TRADER_LLM_MODEL = "moonshotai/kimi-k3"\n', encoding="utf-8")
        (source / "register_evening_task.ps1").write_text("# task\n", encoding="utf-8")
        return source

    def test_fork_clones_state_but_not_credentials(self):
        self._seed_source()
        market = self.tmp / "data"
        for relative in ("daily/600000.csv", "intraday/1m/600000.csv", "snapshots/sample.csv"):
            path = market / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic shared data sentinel\n")
        before = {p.relative_to(market): (p.read_bytes(), p.stat().st_mtime_ns) for p in market.rglob("*") if p.is_file()}
        marker = fork_profile(profile="glm", source_profile="kimi")
        self.assertEqual(before, {p.relative_to(market): (p.read_bytes(), p.stat().st_mtime_ns) for p in market.rglob("*") if p.is_file()})
        target = profile_state_dir("glm")
        self.assertTrue((target / "ledger.json").is_file())
        self.assertTrue((target / "plan_latest.json").is_file())
        self.assertTrue((target / "PROFILE_FORK.json").is_file())
        self.assertFalse((target / "register_evening_task.ps1").exists())
        self.assertEqual(marker["ledger_id"], "ai_trader_100k_glm")
        forked = json.loads((target / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(forked["ledger_id"], "ai_trader_100k_glm")
        self.assertEqual((forked.get("mandate") or {}).get("ledger_id"), "ai_trader_100k_glm")
        env = (target / "llm.env").read_text(encoding="utf-8")
        self.assertIn("z-ai/glm-5.3", env)
        self.assertIn('ANTHROPIC_AUTH_TOKEN = ""', env)
        self.assertNotIn("kimi-k3", env)
        self.assertEqual(load_ledger(target / "ledger.json").get("ledger_id"), "ai_trader_100k_glm")

    def test_fork_refuses_existing_target(self):
        self._seed_source()
        fork_profile(profile="glm", source_profile="kimi")
        with self.assertRaises(FileExistsError):
            fork_profile(profile="glm", source_profile="kimi")


if __name__ == "__main__":
    unittest.main()
