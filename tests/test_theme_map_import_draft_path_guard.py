from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.quant_api_server as quant_api_server
from scripts.quant_api_server import app


class ThemeMapImportDraftDirectCallPathGuardTests(unittest.TestCase):
    """The underlying generator must constrain `output_path`, not only the API.

    A direct function call to generate_import_draft must refuse to write a draft
    CSV outside the project tree (via `..` or an absolute external path) or to a
    non-CSV file, so the safety does not depend on the API layer alone.
    """

    def test_parent_traversal_output_path_is_rejected(self) -> None:
        from quantdesk.theme_map_import_draft import generate_import_draft

        with self.assertRaises(ValueError):
            generate_import_draft(output_path="../evil.csv")

    def test_absolute_external_output_path_is_rejected(self) -> None:
        from quantdesk.theme_map_import_draft import generate_import_draft

        for external in ("C:/Windows/Temp/evil.csv", "/tmp/evil.csv"):
            with self.assertRaises(ValueError, msg=external):
                generate_import_draft(output_path=external)

    def test_non_csv_output_path_is_rejected(self) -> None:
        from quantdesk.theme_map_import_draft import generate_import_draft

        with self.assertRaises(ValueError):
            generate_import_draft(output_path="data/evil.txt")

    def test_resolver_accepts_project_relative_csv(self) -> None:
        from quantdesk.theme_map_import_draft import (
            PROJECT_ROOT,
            STOCK_THEME_MAP_IMPORT_CSV,
            _resolve_project_csv_path,
        )

        resolved = _resolve_project_csv_path("results/research/draft.csv", STOCK_THEME_MAP_IMPORT_CSV)
        self.assertEqual(resolved.suffix.lower(), ".csv")
        resolved.relative_to(PROJECT_ROOT.resolve())  # raises if outside project

    def test_resolver_empty_falls_back_to_default(self) -> None:
        from quantdesk.theme_map_import_draft import (
            STOCK_THEME_MAP_IMPORT_CSV,
            _resolve_project_csv_path,
        )

        self.assertEqual(
            _resolve_project_csv_path("", STOCK_THEME_MAP_IMPORT_CSV),
            STOCK_THEME_MAP_IMPORT_CSV.resolve(),
        )


class ThemeMapImportDraftPathGuardTests(unittest.TestCase):
    """The import-draft endpoint must constrain `output` to project CSV paths.

    Mirrors the sister /api/theme-map/import guard so a POST cannot write a CSV
    outside the project directory via `..` traversal or an absolute path.
    """

    def setUp(self) -> None:
        sandbox_parent = Path(__file__).resolve().parents[1] / ".tmp_tests"
        sandbox_parent.mkdir(parents=True, exist_ok=True)
        self._temp_dir = tempfile.TemporaryDirectory(
            prefix="theme-map-path-guard-",
            dir=sandbox_parent,
        )
        self.addCleanup(self._temp_dir.cleanup)
        self.import_csv = Path(self._temp_dir.name) / "stock_theme_map_import.csv"
        self._import_path_patch = mock.patch.object(
            quant_api_server,
            "STOCK_THEME_MAP_IMPORT_CSV",
            self.import_csv,
        )
        self._import_path_patch.start()
        self.addCleanup(self._import_path_patch.stop)
        self.client = app.test_client()

    def test_parent_traversal_output_is_rejected(self) -> None:
        response = self.client.post(
            "/api/theme-map/import-draft",
            json={"output": "../evil.csv"},
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload.get("ok"))
        self.assertEqual(payload.get("status"), "bad_output_path")

    def test_absolute_external_output_is_rejected(self) -> None:
        for external in ("C:/Windows/Temp/evil.csv", "/tmp/evil.csv"):
            response = self.client.post(
                "/api/theme-map/import-draft",
                json={"output": external},
            )
            self.assertEqual(response.status_code, 400, external)
            self.assertEqual(response.get_json().get("status"), "bad_output_path", external)

    def test_non_csv_output_is_rejected(self) -> None:
        response = self.client.post(
            "/api/theme-map/import-draft",
            json={"output": "data/evil.txt"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json().get("status"), "bad_output_path")

    def test_empty_output_falls_back_to_default_and_does_not_400(self) -> None:
        # A legal request (no output override) must not be rejected by the guard;
        # it may still warn/degrade on data, but never returns bad_output_path.
        response = self.client.post("/api/theme-map/import-draft", json={})
        self.assertNotEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertNotEqual(payload.get("status"), "bad_output_path")
        self.assertEqual(Path(payload["absolute_path"]), self.import_csv.resolve())
        self.assertTrue(self.import_csv.exists())


if __name__ == "__main__":
    unittest.main()
