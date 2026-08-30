from __future__ import annotations

import unittest
from pathlib import Path

DEFERRED_MARKER = "current research-only v580 scope forbids release packaging"


def release_packaging_deferred(root: Path) -> bool:
    """Return True when the current acceptance scope explicitly defers packaging."""

    for path in (root / "AGENTS.md", root / "scripts" / "self_check.py"):
        if path.exists() and DEFERRED_MARKER in path.read_text(encoding="utf-8", errors="ignore"):
            return True
    return False


def skip_if_release_packaging_deferred(testcase: unittest.TestCase, root: Path) -> None:
    if release_packaging_deferred(root):
        testcase.skipTest("release packaging is deferred by the current v580 research-only scope")
