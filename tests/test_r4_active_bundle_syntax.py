"""The ACTIVE 8000 bundle must parse. Runs node --check for real.

Why this exists: the retired stack had two `node --check app/static/app.js`
gates. Both point at a bundle this repo no longer ships, and a scan of tests/
found no test that runs node --check against app/static/js/daily_research.js.
Retiring the old gates without adding this one would leave the only bundle the
product actually serves with no syntax gate at all.

Asserting that .gitlab-ci.yml *mentions* the command is not a substitute: CI text
cannot catch a syntax error introduced locally.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "app" / "static" / "js" / "daily_research.js"
TEMPLATE = ROOT / "app" / "templates" / "daily_research.html"
ECHARTS = ROOT / "app" / "static" / "echarts.min.js"


def test_active_bundle_exists_and_is_not_empty() -> None:
    assert BUNDLE.is_file(), f"active bundle missing: {BUNDLE}"
    assert BUNDLE.stat().st_size > 10_000, "active bundle is implausibly small"


def test_active_template_and_vendored_chart_library_exist() -> None:
    """The template loads echarts.min.js; a missing vendor file blanks every chart."""
    assert TEMPLATE.is_file(), f"active template missing: {TEMPLATE}"
    assert ECHARTS.is_file(), f"vendored chart library missing: {ECHARTS}"
    assert "echarts.min.js" in TEMPLATE.read_text(encoding="utf-8", errors="replace"), (
        "the active template no longer references the vendored chart library"
    )


def test_node_check_passes_on_the_active_bundle() -> None:
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    completed = subprocess.run(
        ["node", "--check", "app/static/js/daily_research.js"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    assert completed.returncode == 0, (
        "node --check failed on the ACTIVE bundle:\n" + (completed.stderr or "")
    )


def test_node_check_would_actually_fail_on_broken_syntax(tmp_path: Path) -> None:
    """Proves the gate can fail, so a passing run means something."""
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    broken = tmp_path / "broken.js"
    broken.write_text("function oops( {\n", encoding="utf-8")
    completed = subprocess.run(
        ["node", "--check", broken.name],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    assert completed.returncode != 0, "node --check accepted invalid JavaScript"


def test_no_test_asserts_the_retired_bundle_syntax() -> None:
    """After retirement, nothing may gate on app/static/app.js parsing."""
    here = Path(__file__).name
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.name == here:
            continue  # this file names the retired path only to forbid it
        text = path.read_text(encoding="utf-8", errors="replace")
        if '"app/static/app.js"' in text and "--check" in text:
            offenders.append(path.name)
    assert not offenders, (
        "these tests still run node --check on the retired bundle: "
        + ", ".join(offenders)
    )
