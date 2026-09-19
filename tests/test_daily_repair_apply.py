from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk.daily_integrity_check import DAILY_COLUMNS  # noqa: E402
from quantdesk.daily_repair_apply import apply_daily_repair_bundle  # noqa: E402
from scripts.apply_daily_repair_bundle import main as apply_main  # noqa: E402

SYMBOL = "002633"


def _row(day, symbol=SYMBOL, close="10.0"):
    return [day, symbol, "9.9", "10.5", "9.8", close, "1000.0", "10000.0",
            "0.1", "1.0", "raw", "sina_spot_close", f"{day}T16:00:00"]


def _bytes(rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(DAILY_COLUMNS)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DailyRepairApplyTests(unittest.TestCase):
    def _make(self, root: Path, live: bytes, candidate: bytes, **overrides):
        daily = root / "daily"
        daily.mkdir(exist_ok=True)
        (daily / f"{SYMBOL}.csv").write_bytes(live)
        bundle = root / "bundle"
        (bundle / "candidates").mkdir(parents=True, exist_ok=True)
        (bundle / "manifests").mkdir(exist_ok=True)
        (bundle / "candidates" / f"{SYMBOL}.csv").write_bytes(candidate)
        item = {
            "symbol": SYMBOL,
            "status": "structural_candidate_only",
            "candidate_path": f"candidates/{SYMBOL}.csv",
            "candidate_sha256": _sha(candidate),
            "observed_original_sha256": _sha(live),
            "history_preserved": True,
            "coverage_complete": False,
            "input_hashes_match_at_final_check": True,
        }
        item.update(overrides)
        (bundle / "manifests" / f"{SYMBOL}.json").write_text(json.dumps(item), encoding="utf-8")
        (bundle / "manifest.json").write_text(json.dumps({"items": [item]}), encoding="utf-8")
        return bundle, daily

    def _corrupted_live(self) -> bytes:
        data = _bytes([_row("2026-09-07"), _row("2026-09-08")])
        return data.replace(b"2026-09-08,002633", b"\x00" * 111 + b"\n2026-09-08,002633", 1)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = self._corrupted_live()
            candidate = _bytes([_row("2026-09-07"), _row("2026-09-08")])
            bundle, daily = self._make(root, live, candidate)
            report = apply_daily_repair_bundle(bundle_dir=bundle, daily_dir=daily)
            self.assertEqual(report["mode"], "dry_run")
            self.assertEqual(report["rows"][0]["status"], "would_replace")
            self.assertIn("dry_run", report["rows"][0]["reasons"])
            self.assertFalse(report["canonical_write"])
            self.assertEqual((daily / f"{SYMBOL}.csv").read_bytes(), live)

    def test_apply_without_confirmation_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = self._corrupted_live()
            candidate = _bytes([_row("2026-09-07"), _row("2026-09-08")])
            bundle, daily = self._make(root, live, candidate)
            report = apply_daily_repair_bundle(bundle_dir=bundle, daily_dir=daily, apply=True)
            self.assertEqual(report["mode"], "dry_run")
            self.assertIn("confirm_no_writer_missing", report["rows"][0]["reasons"])
            self.assertEqual((daily / f"{SYMBOL}.csv").read_bytes(), live)

    def test_apply_replaces_only_cas_matching_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = self._corrupted_live()
            candidate = _bytes([_row("2026-09-07"), _row("2026-09-08")])
            bundle, daily = self._make(root, live, candidate)
            report = apply_daily_repair_bundle(
                bundle_dir=bundle, daily_dir=daily, apply=True, confirm_no_writer=True,
            )
            self.assertEqual(report["rows"][0]["status"], "applied")
            self.assertTrue(report["canonical_write"])
            self.assertEqual((daily / f"{SYMBOL}.csv").read_bytes(), candidate)
            self.assertEqual(report["rows"][0]["after_sha256"], _sha(candidate))

    def test_live_change_since_prepare_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = self._corrupted_live()
            candidate = _bytes([_row("2026-09-07"), _row("2026-09-08")])
            bundle, daily = self._make(root, live, candidate)
            (daily / f"{SYMBOL}.csv").write_bytes(live + b"2026-09-09,002633,1,1,1,1,1,1,0,0,raw,s,ts\n")
            report = apply_daily_repair_bundle(
                bundle_dir=bundle, daily_dir=daily, apply=True, confirm_no_writer=True,
            )
            self.assertEqual(report["rows"][0]["status"], "refused")
            self.assertIn("live_changed_since_prepare", report["rows"][0]["reasons"])

    def test_blocked_and_unpreserved_and_incomplete_are_refused(self):
        cases = {
            "source_not_usable": {"status": "blocked"},
            "history_not_preserved": {"history_preserved": False},
            "coverage_incomplete": None,
        }
        for expected, overrides in cases.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                live = self._corrupted_live()
                candidate = _bytes([_row("2026-09-07"), _row("2026-09-08")])
                bundle, daily = self._make(root, live, candidate, **(overrides or {}))
                report = apply_daily_repair_bundle(
                    bundle_dir=bundle, daily_dir=daily, apply=True, confirm_no_writer=True,
                    allow_incomplete=expected != "coverage_incomplete",
                )
                self.assertEqual(report["rows"][0]["status"], "refused")
                self.assertIn(expected, " ".join(report["rows"][0]["reasons"]))

    def test_defective_candidates_are_refused(self):
        for expected, mutate in (
            ("candidate_contains_nul", lambda data: data.replace(b"9.9", b"\x00\x00\x00", 1)),
            ("candidate_sha256_mismatch", None),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                live = self._corrupted_live()
                candidate = _bytes([_row("2026-09-07"), _row("2026-09-08")])
                overrides = {}
                if mutate is not None:
                    candidate = mutate(candidate)
                    overrides["candidate_sha256"] = _sha(candidate)
                else:
                    overrides["candidate_sha256"] = "0" * 64
                bundle, daily = self._make(root, live, candidate, **overrides)
                report = apply_daily_repair_bundle(
                    bundle_dir=bundle, daily_dir=daily, apply=True, confirm_no_writer=True,
                )
                self.assertEqual(report["rows"][0]["status"], "refused")
                self.assertIn(expected, " ".join(report["rows"][0]["reasons"]))

    def test_already_applied_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = _bytes([_row("2026-09-07"), _row("2026-09-08")])
            bundle, daily = self._make(root, candidate, candidate)
            report = apply_daily_repair_bundle(
                bundle_dir=bundle, daily_dir=daily, apply=True, confirm_no_writer=True,
            )
            self.assertEqual(report["rows"][0]["status"], "already_applied")
            self.assertFalse(report["canonical_write"])

    def test_cli_dry_run_exit_code_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = self._corrupted_live()
            candidate = _bytes([_row("2026-09-07"), _row("2026-09-08")])
            bundle, daily = self._make(root, live, candidate)
            self.assertEqual(apply_main(["--bundle", str(bundle), "--daily-dir", str(daily), "--json", "false"]), 0)

    def test_cli_refusal_exit_code_is_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = self._corrupted_live()
            candidate = _bytes([_row("2026-09-07"), _row("2026-09-08")])
            bundle, daily = self._make(root, live, candidate, status="blocked")
            self.assertEqual(apply_main(["--bundle", str(bundle), "--daily-dir", str(daily), "--json", "false"]), 3)


if __name__ == "__main__":
    unittest.main()
