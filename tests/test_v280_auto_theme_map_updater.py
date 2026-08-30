"""Tests for v2.8.0 auto theme map updater."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.theme_map_updater import (
    THEME_MAP_COLUMNS,
    _is_included,
    _zfill6,
    theme_map_preview,
    theme_map_status,
    update_theme_map,
)


class SymbolNormTests(unittest.TestCase):
    def test_zfill6(self) -> None:
        self.assertEqual(_zfill6("601778"), "601778")
        self.assertEqual(_zfill6("1778"), "001778")
        self.assertEqual(_zfill6("000001"), "000001")

    def test_included_values(self) -> None:
        for v in ("true", "True", "1", "yes", "Y"):
            self.assertTrue(_is_included(v))
        for v in ("false", "0", "no", "", None):
            self.assertFalse(_is_included(v))


class UpdateThemeMapTests(unittest.TestCase):
    @contextmanager
    def _isolated_paths(
        self,
        tmp_path: Path,
        *,
        output_name: str = "out.csv",
        pending_name: str = "pending.csv",
        spot_path: Path | None = None,
    ) -> Iterator[None]:
        """Keep every updater input/output, including sidecars, in one sandbox."""

        paths = {
            "LOW_PRICE_SYMBOLS_CSV": tmp_path / "low_price_learning_symbols.csv",
            "THEME_MAP_CSV": tmp_path / output_name,
            "THEME_MAP_PENDING_CSV": tmp_path / pending_name,
            "THEME_MAP_STATUS_JSON": tmp_path / "stock_theme_map_status.json",
            "THEME_MAP_IMPORT_CSV": tmp_path / "stock_theme_map_import.csv",
            "SPOT_CACHE_CSV": spot_path or (tmp_path / "nonexist.csv"),
        }
        with ExitStack() as stack:
            for name, path in paths.items():
                stack.enter_context(
                    mock.patch(f"quantdesk.theme_map_updater.{name}", path)
                )
            yield

    def _write_universe(self, tmp: Path, symbols: list[dict]) -> Path:
        path = tmp / "low_price_learning_symbols.csv"
        df = pd.DataFrame(symbols)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path

    def _write_theme_map(self, tmp: Path, rows: list[dict]) -> Path:
        path = tmp / "stock_theme_map.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=THEME_MAP_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_only_included_processed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            universe = tmp_path / "low_price_learning_symbols.csv"
            pd.DataFrame([
                {"symbol": "601778", "name": "晶科科技", "included": "true"},
                {"symbol": "000001", "name": "平安银行", "included": "false"},
            ]).to_csv(universe, index=False, encoding="utf-8-sig")

            with self._isolated_paths(tmp_path):
                result = update_theme_map(
                    source="local",
                    output_path=tmp_path / "out.csv",
                    pending_path=tmp_path / "pending.csv",
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result["stats"]["total"], 1)

    def test_symbol_6_digits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            universe = tmp_path / "low_price_learning_symbols.csv"
            pd.DataFrame([{"symbol": "1778", "name": "test", "included": "true"}]).to_csv(universe, index=False)
            with self._isolated_paths(tmp_path):
                result = update_theme_map(
                    source="local",
                    output_path=tmp_path / "out.csv",
                    pending_path=tmp_path / "pending.csv",
                )
            df = pd.read_csv(tmp_path / "out.csv", dtype=str)
            self.assertEqual(df.iloc[0]["symbol"], "001778")

    def test_manual_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            universe = tmp_path / "low_price_learning_symbols.csv"
            pd.DataFrame([{"symbol": "601778", "name": "晶科科技", "included": "true"}]).to_csv(universe, index=False)
            existing = tmp_path / "theme_map.csv"
            self._write_theme_map(tmp_path, [{"symbol": "601778", "name": "晶科科技", "sector": "电力设备", "industry": "光伏", "source": "manual", "updated_at": "2026-01-01"}])
            existing = tmp_path / "stock_theme_map.csv"

            with self._isolated_paths(tmp_path, output_name=existing.name):
                result = update_theme_map(
                    source="local",
                    overwrite_manual=False,
                    output_path=existing,
                    pending_path=tmp_path / "pending.csv",
                )
            self.assertEqual(result["stats"]["manual"], 1)

    def test_overwrite_manual_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            universe = tmp_path / "low_price_learning_symbols.csv"
            pd.DataFrame([{"symbol": "601778", "name": "晶科科技", "included": "true"}]).to_csv(universe, index=False)
            existing = tmp_path / "stock_theme_map.csv"
            self._write_theme_map(tmp_path, [{"symbol": "601778", "name": "晶科科技", "sector": "电力设备", "source": "manual"}])

            with self._isolated_paths(tmp_path, output_name=existing.name):
                result = update_theme_map(
                    source="local",
                    overwrite_manual=True,
                    output_path=existing,
                    pending_path=tmp_path / "pending.csv",
                )
            # With overwrite, manual count should be 0
            self.assertEqual(result["stats"]["manual"], 0)

    def test_akshare_not_installed_no_crash(self) -> None:
        with mock.patch.dict("sys.modules", {"akshare": None}):
            from quantdesk.theme_map_updater import _try_akshare_concept, _try_akshare_industry
            self.assertEqual(_try_akshare_industry(), {})
            self.assertEqual(_try_akshare_concept(), {})

    def test_concept_tags_semicolon_separated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            universe = tmp_path / "low_price_learning_symbols.csv"
            spot = tmp_path / "spot.csv"
            pd.DataFrame([{"symbol": "601778", "name": "晶科科技", "included": "true"}]).to_csv(universe, index=False)
            pd.DataFrame([{"代码": "601778", "概念": "光伏;新能源;碳中和"}]).to_csv(spot, index=False)

            with self._isolated_paths(tmp_path, spot_path=spot):
                result = update_theme_map(
                    source="local",
                    output_path=tmp_path / "out.csv",
                    pending_path=tmp_path / "pending.csv",
                )
            df = pd.read_csv(tmp_path / "out.csv", dtype=str)
            self.assertIn(";", str(df.iloc[0].get("concept_tags") or ""))

    def test_unknown_goes_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            universe = tmp_path / "low_price_learning_symbols.csv"
            pd.DataFrame([{"symbol": "999999", "name": "虚拟股", "included": "true"}]).to_csv(universe, index=False)

            with self._isolated_paths(tmp_path):
                result = update_theme_map(
                    source="local",
                    output_path=tmp_path / "out.csv",
                    pending_path=tmp_path / "pending.csv",
                )
            self.assertGreater(result["stats"]["unknown"], 0)
            self.assertTrue((tmp_path / "pending.csv").exists())


class StatusAndPreviewTests(unittest.TestCase):
    def test_status_returns_ok(self) -> None:
        result = theme_map_status()
        self.assertTrue(result["ok"])
        self.assertIn("exists", result)

    def test_preview_returns_list(self) -> None:
        result = theme_map_preview(5)
        self.assertIsInstance(result, list)


class CLITests(unittest.TestCase):
    def test_help(self) -> None:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "update_stock_theme_map.py"), "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--source", result.stdout)
        self.assertIn("--overwrite-manual", result.stdout)

    def test_local_source_no_network(self) -> None:
        import subprocess

        protected = tuple(
            PROJECT_ROOT / "data" / "universe" / name
            for name in (
                "stock_theme_map.csv",
                "stock_theme_map_pending.csv",
                "stock_theme_map_status.json",
                "stock_theme_map_import.csv",
            )
        )
        before = {path: path.read_bytes() for path in protected if path.exists()}
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            output = temp_root / "stock_theme_map.csv"
            pending = temp_root / "stock_theme_map_pending.csv"
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "update_stock_theme_map.py"),
                    "--source",
                    "local",
                    "--limit",
                    "3",
                    "--output",
                    str(output),
                    "--pending-output",
                    str(pending),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ok", result.stdout.lower() + result.stderr.lower())
            self.assertTrue(output.exists())
            self.assertTrue(pending.exists())

        after = {path: path.read_bytes() for path in protected if path.exists()}
        self.assertEqual(before, after)


class VersionTests(unittest.TestCase):
    def test_version(self) -> None:
        from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION
        self.assertTrue(APP_VERSION.startswith("2.8.") or APP_VERSION.startswith("2.9.") or APP_VERSION.startswith("3.0.") or APP_VERSION.startswith("3.1.") or APP_VERSION.startswith("3.2.") or APP_VERSION.startswith("3.3.") or APP_VERSION.startswith("3.4.") or APP_VERSION.startswith("3.5.") or APP_VERSION.startswith("3.6.") or APP_VERSION.startswith("3.7."))
        self.assertGreaterEqual(API_SCHEMA_VERSION, "2.8.0")


if __name__ == "__main__":
    unittest.main()
