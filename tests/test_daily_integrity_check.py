from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk.daily_integrity_check import (  # noqa: E402
    DAILY_COLUMNS,
    inspect_daily_file,
    run_daily_integrity_check,
)
from scripts.check_daily_integrity import main as check_main  # noqa: E402


def _row(day, symbol, close="10.0", adjust="raw"):
    return [day, symbol, "9.9", "10.5", "9.8", close, "1000.0", "10000.0",
            "0.1", "1.0", adjust, "sina_spot_close", f"{day}T16:00:00"]


def _csv_bytes(rows, header=DAILY_COLUMNS):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


class InspectDailyFileTests(unittest.TestCase):
    def _write(self, root: Path, name: str, data: bytes) -> Path:
        path = root / name
        path.write_bytes(data)
        return path

    def test_clean_file_has_no_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "002633.csv", _csv_bytes([
                _row("2026-09-07", "002633"), _row("2026-09-08", "002633"),
            ]))
            payload = inspect_daily_file(path)
        self.assertEqual(payload["issue_codes"], [])
        self.assertEqual(payload["nul_byte_count"], 0)
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual(payload["last_date"], "2026-09-08")

    def test_nul_only_inserted_record_is_flagged(self):
        data = _csv_bytes([_row("2026-09-07", "002633"), _row("2026-09-08", "002633")])
        corrupted = data.replace(
            b"2026-09-08,002633",
            b"\x00" * 111 + b"\n2026-09-08,002633",
            1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "002633.csv", corrupted)
            payload = inspect_daily_file(path)
        self.assertIn("nul_bytes", payload["issue_codes"])
        self.assertIn("column_mismatch", payload["issue_codes"])
        self.assertEqual(payload["nul_record_lines"], [3])
        self.assertEqual(payload["nul_byte_count"], 111)

    def test_column_mismatch_and_symbol_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _csv_bytes([_row("2026-09-07", "600000")])
            bad = data.replace(b"600000", b"600000", 1) + b"2026-09-08,5\n"
            path = self._write(Path(tmp), "002633.csv", bad)
            payload = inspect_daily_file(path)
        self.assertIn("column_mismatch", payload["issue_codes"])
        self.assertIn("symbol_mismatch", payload["issue_codes"])

    def test_duplicate_and_noncanonical_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _csv_bytes([_row("2026-09-08", "002633"), _row("2026-09-08", "002633")])
            path = self._write(Path(tmp), "002633.csv", data)
            payload = inspect_daily_file(path)
            self.assertIn("duplicate_dates", payload["issue_codes"])

            bad = _csv_bytes([_row("2026-9-8", "002633")])
            path2 = self._write(Path(tmp), "002634.csv", bad)
            payload2 = inspect_daily_file(path2)
        self.assertIn("noncanonical_date", payload2["issue_codes"])

    def test_header_and_empty_are_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_header = _csv_bytes([_row("2026-09-08", "002633")], header=("date", "symbol"))
            payload = inspect_daily_file(self._write(Path(tmp), "002633.csv", bad_header))
            empty = inspect_daily_file(self._write(Path(tmp), "002635.csv", b""))
        self.assertIn("header_mismatch", payload["issue_codes"])
        self.assertEqual(empty["issue_codes"], ["empty_file"])


class RunDailyIntegrityCheckTests(unittest.TestCase):
    def test_scan_reports_flagged_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "002633.csv").write_bytes(_csv_bytes([_row("2026-09-08", "002633")]))
            (root / "002634.csv").write_bytes(b"2026-09-08,002634,1\n")
            payload = run_daily_integrity_check(root, universe_symbols=["002633", "002634", "999999"])
        self.assertEqual(payload["scanned_files"], 2)
        self.assertEqual(payload["flagged_files"], 1)
        self.assertEqual(payload["clean_files"], 1)
        self.assertEqual(payload["missing_universe_files"], ["999999"])
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["read_only"])

    def test_stale_before_flags_old_last_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "002633.csv").write_bytes(_csv_bytes([_row("2026-09-08", "002633")]))
            payload = run_daily_integrity_check(root, stale_before="2026-09-15")
        self.assertEqual(payload["stale_count"], 1)
        self.assertEqual(payload["stale_sample"], ["002633"])


class CheckDailyIntegrityCliTests(unittest.TestCase):
    def test_exit_codes_and_report_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = root / "daily"
            daily.mkdir()
            (daily / "002633.csv").write_bytes(_csv_bytes([_row("2026-09-08", "002633")]))
            self.assertEqual(check_main(["--daily-dir", str(daily), "--json", "false"]), 0)

            (daily / "002634.csv").write_bytes(b"2026-09-08,002634,1\n")
            report = root / "report.json"
            code = check_main(["--daily-dir", str(daily), "--json", "false", "--output", str(report)])
            self.assertEqual(code, 3)
            self.assertTrue(report.is_file())
            written = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(written["flagged_files"], 1)

    def test_missing_directory_is_usage_error(self):
        code = check_main(["--daily-dir", str(Path(tempfile.gettempdir()) / "definitely-absent-xyz"), "--json", "false"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
