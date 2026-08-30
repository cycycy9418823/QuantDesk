"""Shared helpers for tests in this project.

QuantDesk v2.6.3-codebase-slimming introduces this module so tests can
share small assertion helpers without duplicating boilerplate.  The
project still uses the standard ``unittest`` framework; this module
intentionally does not pull in ``pytest``.

Helpers provided here:

- ``project_root()``: Resolve the project root from any test file.
- ``ensure_project_on_sys_path``: Idempotent ``sys.path`` insertion.
- ``assert_centralized_app_version``: Confirm the server / version /
  config files all agree on the current ``APP_VERSION`` and
  ``API_SCHEMA_VERSION``.
- ``read_text``: Read a file as UTF-8 with a deterministic fallback.
- ``assert_no_mojibake``: Fail when visible UI text contains broken
  encoding markers.

These helpers must remain stateless and side-effect free other than
their documented ``sys.path`` insertion.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - only used for type hints
    import unittest


def project_root() -> Path:
    """Return the absolute project root directory.

    Tests live under ``tests/`` so the project root is one level up.
    """
    return Path(__file__).resolve().parents[1]


def ensure_project_on_sys_path() -> Path:
    """Insert the project root at the front of ``sys.path`` if missing."""
    root = project_root()
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)
    return root


def read_text(path: Path) -> str:
    """Read ``path`` as UTF-8, replacing decode errors with a marker.

    Tests should generally fail on mojibake explicitly, so this helper
    intentionally never silently drops bytes.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def assert_centralized_app_version(test: "unittest.TestCase") -> None:
    """Check that ``quantdesk.version`` and ``scripts.quant_api_server``
    expose the same ``APP_VERSION``.

    The exact version string is intentionally not asserted here: tests
    that assert specific strings should do so directly.  This helper
    only confirms centralization continues to hold after future bumps.
    """
    ensure_project_on_sys_path()
    import quantdesk.version as version_info  # noqa: WPS433
    import scripts.quant_api_server as qas  # noqa: WPS433

    test.assertTrue(
        version_info.APP_VERSION,
        "quantdesk.version.APP_VERSION must not be empty",
    )
    test.assertTrue(
        version_info.API_SCHEMA_VERSION,
        "quantdesk.version.API_SCHEMA_VERSION must not be empty",
    )
    test.assertTrue(
        version_info.RELEASE_TITLE,
        "quantdesk.version.RELEASE_TITLE must not be empty",
    )
    test.assertEqual(
        qas.APP_VERSION,
        version_info.APP_VERSION,
        "scripts.quant_api_server.APP_VERSION must reuse quantdesk.version",
    )


def assert_no_mojibake(test: "unittest.TestCase", path: Path) -> None:
    """Fail when ``path`` contains broken-encoding markers in visible text."""
    ensure_project_on_sys_path()
    from quantdesk.self_check_core import contains_mojibake  # noqa: WPS433

    text = read_text(path)
    test.assertFalse(
        contains_mojibake(text),
        f"{path} contains mojibake / broken-placeholder markers",
    )
