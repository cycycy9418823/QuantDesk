from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_trader.daytime_intent import (  # noqa: E402
    INTENT_SCHEMA,
    load_daytime_intents_for,
    observe_intents,
)
from ai_trader.ledger import empty_ledger  # noqa: E402
from ai_trader.paths import CyclePaths  # noqa: E402
from ai_trader.watchlist import empty_watchlist  # noqa: E402

NOW = datetime(2026, 9, 16, 10, 30, 0)


class DaytimeIntentTests(unittest.TestCase):
    def _paths(self, root: Path) -> CyclePaths:
        state = root / "state"
        state.mkdir(exist_ok=True)
        return CyclePaths(
            ledger=state / "ledger.json",
            plan_latest=state / "plan_latest.json",
            watchlist=state / "watchlist.json",
            daytime_intents=state / "daytime_intents.jsonl",
            daytime_notes=state / "daytime_notes.jsonl",
            daytime_session=state / "daytime_session.json",
        )

    def _write_plan(self, paths: CyclePaths, intents: list[dict]) -> None:
        paths.plan_latest.write_text(json.dumps({"intents": intents}, ensure_ascii=False), encoding="utf-8")

    def _write_watchlist(self, paths: CyclePaths, symbols: list[str]) -> None:
        book = empty_watchlist()
        book["items"] = [{"symbol": symbol} for symbol in symbols]
        paths.watchlist.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")

    def _write_ledger(self, paths: CyclePaths, positions: list[dict]) -> None:
        ledger = empty_ledger()
        ledger["positions"] = positions
        paths.ledger.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")

    def _spot(self, symbol: str, **overrides) -> dict:
        row = {"symbol": symbol, "name": f"股票{symbol}", "price": "9.00",
               "open": "9.05", "pct_chg": "-1.10"}
        row.update(overrides)
        return row

    def test_would_buy_intent_when_preset_touch_is_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._write_plan(paths, [{"symbol": "600000", "side": "buy",
                                      "ref_close": 9.1, "preset_touch_price": 9.01,
                                      "abandon_if_open_gap_pct": 5.0}])
            result = observe_intents(Path(tmp), now=NOW, snapshot_rows=[self._spot("600000", price="9.00")], paths=paths)
        self.assertEqual(result["new_intents"], 1)
        event = result["events"][0]
        self.assertEqual(event["schema_version"], INTENT_SCHEMA)
        self.assertEqual(event["side"], "buy")
        self.assertEqual(event["precondition"], "preset_touch_price")
        self.assertEqual(event["status"], "intent_only")
        self.assertFalse(event["filled"])
        self.assertFalse(event["ledger_written"])
        self.assertFalse(event["plan_written"])
        self.assertFalse(event["watchlist_written"])

    def test_no_intent_when_level_not_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._write_plan(paths, [{"symbol": "600000", "side": "buy",
                                      "ref_close": 9.1, "preset_touch_price": 9.01,
                                      "abandon_if_open_gap_pct": 5.0}])
            result = observe_intents(Path(tmp), now=NOW, snapshot_rows=[self._spot("600000", price="9.50", open="9.40")], paths=paths)
        self.assertEqual(result["new_intents"], 0)

    def test_open_gap_abandon_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._write_plan(paths, [{"symbol": "600000", "side": "buy",
                                      "ref_close": 9.10, "preset_touch_price": 8.90,
                                      "abandon_if_open_gap_pct": 5.0}])
            result = observe_intents(Path(tmp), now=NOW,
                                     snapshot_rows=[self._spot("600000", price="9.60", open="9.60")], paths=paths)
        codes = {event["precondition"] for event in result["events"]}
        self.assertIn("open_gap_abandon", codes)

    def test_day_low_touch_from_five_minute_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._write_plan(paths, [{"symbol": "600000", "side": "buy",
                                      "ref_close": 9.1, "preset_touch_price": 9.01}])
            slot_dir = root / "data" / "snapshots" / "2026-09-16"
            slot_dir.mkdir(parents=True)
            (slot_dir / "0945.csv").write_text(
                "snapshot_date,snapshot_time,symbol,name,price,pct_chg,open,high,low\n"
                "2026-09-16,0945,600000,股票600000,9.30,-1.0,9.20,9.35,9.00\n",
                encoding="utf-8-sig",
            )
            result = observe_intents(root, now=NOW,
                                     snapshot_rows=[self._spot("600000", price="9.30", open="9.20")],
                                     paths=paths)
        self.assertEqual(result["new_intents"], 1)
        event = result["events"][0]
        self.assertEqual(event["precondition"], "preset_touch_price")
        self.assertEqual(event["observed_low"], 9.0)

    def test_dedup_within_the_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._write_plan(paths, [{"symbol": "600000", "side": "buy",
                                      "ref_close": 9.1, "preset_touch_price": 9.01,
                                      "abandon_if_open_gap_pct": 5.0}])
            rows = [self._spot("600000", price="9.00")]
            first = observe_intents(Path(tmp), now=NOW, snapshot_rows=rows, paths=paths)
            second = observe_intents(Path(tmp), now=NOW, snapshot_rows=rows, paths=paths)
            written = load_daytime_intents_for("2026-09-16", intents_path=paths.daytime_intents)
        self.assertEqual(first["new_intents"], 1)
        self.assertEqual(second["new_intents"], 0)
        self.assertEqual(len(written), 1)

    def test_never_writes_ledger_plan_or_watchlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._write_plan(paths, [{"symbol": "600000", "side": "buy",
                                      "ref_close": 9.1, "preset_touch_price": 9.01}])
            self._write_watchlist(paths, ["600519"])
            self._write_ledger(paths, [{"symbol": "600519", "quantity": 100, "avg_cost": 10.0}])
            before = {name: paths.__getattribute__(name).read_bytes()
                      for name in ("ledger", "plan_latest", "watchlist")}
            result = observe_intents(Path(tmp), now=NOW,
                                     snapshot_rows=[self._spot("600000", price="9.00")], paths=paths)
            after = {name: paths.__getattribute__(name).read_bytes()
                     for name in ("ledger", "plan_latest", "watchlist")}
            intents_exists = paths.daytime_intents.is_file()
        self.assertEqual(result["new_intents"], 1)
        self.assertEqual(before, after)
        self.assertTrue(intents_exists)

    def test_review_intent_when_planned_hold_days_reached(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._write_plan(paths, [])
            self._write_ledger(paths, [{"symbol": "600000", "quantity": 100, "avg_cost": 10.0,
                                        "holding_days": 5, "planned_hold_days": 5}])
            result = observe_intents(Path(tmp), now=NOW,
                                     snapshot_rows=[self._spot("600000", price="10.50")], paths=paths)
        codes = {event["precondition"] for event in result["events"]}
        self.assertIn("planned_hold_days", codes)

    def test_watch_limit_intent_for_watchlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._write_plan(paths, [])
            self._write_watchlist(paths, ["600519"])
            result = observe_intents(Path(tmp), now=NOW,
                                     snapshot_rows=[self._spot("600519", price="100.0", pct_chg="10.00")],
                                     paths=paths)
        self.assertEqual(result["new_intents"], 1)
        self.assertEqual(result["events"][0]["precondition"], "watch_limit_touch")
        self.assertEqual(result["events"][0]["side"], "buy")

    def test_reader_filters_by_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            paths.daytime_intents.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in (
                    {"as_of": "2026-09-15", "symbol": "600000", "side": "buy", "precondition": "x"},
                    {"as_of": "2026-09-16", "symbol": "600000", "side": "buy", "precondition": "y"},
                )) + "\n",
                encoding="utf-8",
            )
            today = load_daytime_intents_for("2026-09-16", intents_path=paths.daytime_intents)
            yesterday = load_daytime_intents_for("2026-09-15", intents_path=paths.daytime_intents)
        self.assertEqual(len(today), 1)
        self.assertEqual(today[0]["precondition"], "y")
        self.assertEqual(len(yesterday), 1)

    def test_write_false_does_not_touch_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._write_plan(paths, [{"symbol": "600000", "side": "buy",
                                      "ref_close": 9.1, "preset_touch_price": 9.01}])
            result = observe_intents(Path(tmp), now=NOW,
                                     snapshot_rows=[self._spot("600000", price="9.00")],
                                     paths=paths, write=False)
        self.assertEqual(result["new_intents"], 1)
        self.assertFalse(paths.daytime_intents.exists())


if __name__ == "__main__":
    unittest.main()
