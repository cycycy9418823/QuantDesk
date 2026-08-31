"""The pre-commit guard must keep the retired port-8765 closure out of this repo.

Why this exists: during the migration an automated backfill twice re-added
members of the retired stack by following reference graphs, because a mention
inside a *dead* self_check function looks identical to a real dependency. Nothing
turned red. The guard in tools/git-hooks/check_staged.py is what makes that loud,
so the guard itself needs a test - otherwise a future edit could quietly empty
its blacklist.

This test never touches the real git index: it drives the guard through a
throwaway repository created under tmp_path.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "tools" / "git-hooks" / "check_staged.py"

# The closure members that must never come back. Kept literal here on purpose:
# if someone trims the guard's own list, this test still knows the full set.
REQUIRED_BLACKLIST = (
    "scripts/quant_app_server.py",
    "scripts/live_monitor.py",
    "scripts/generate_sample_report.py",
    "quantdesk/server_payloads.py",
    "quantdesk/live_monitor.py",
    "app/index.html",
    "app/static/app.js",
    "app/static/styles.css",
    "run_report_sample.cmd",
    "run_ui_sample.cmd",
    "run_ui_sample.sh",
)

# Active assets that live next to the retired ones and must NOT be blacklisted.
MUST_NOT_BE_BLACKLISTED = (
    "app/agent_chat.html",
    "app/static/js/daily_research.js",
    "app/templates/daily_research.html",
    "app/static/echarts.min.js",
)


def _guard_source() -> str:
    assert GUARD.is_file(), f"pre-commit guard missing: {GUARD}"
    return GUARD.read_text(encoding="utf-8", errors="replace")


def _retired_closure_literal(source: str) -> set[str]:
    """Extract the RETIRED_CLOSURE set literal from the guard."""
    match = re.search(r"RETIRED_CLOSURE\s*=\s*\{(.*?)\}", source, re.S)
    assert match, "guard no longer defines a RETIRED_CLOSURE set"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_guard_module_exists_and_defines_a_closure_blacklist() -> None:
    closure = _retired_closure_literal(_guard_source())
    assert closure, "RETIRED_CLOSURE must not be empty"


@pytest.mark.parametrize("path", REQUIRED_BLACKLIST)
def test_every_retired_closure_member_is_blacklisted(path: str) -> None:
    closure = _retired_closure_literal(_guard_source())
    assert path in closure, (
        f"{path} is a retired port-8765 closure member but the pre-commit guard "
        f"would allow it back in"
    )


@pytest.mark.parametrize("path", MUST_NOT_BE_BLACKLISTED)
def test_active_assets_are_not_blacklisted(path: str) -> None:
    """Over-blocking is its own failure: app/agent_chat.html is served by 8000."""
    closure = _retired_closure_literal(_guard_source())
    assert path not in closure, (
        f"{path} is an ACTIVE asset; blacklisting it would block legitimate work"
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "probe_repo"
    (repo / "tools" / "git-hooks").mkdir(parents=True)
    (repo / "tools" / "git-hooks" / "check_staged.py").write_bytes(GUARD.read_bytes())
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "probe@local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "probe"], cwd=repo, check=True)
    return repo


def _run_guard(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(repo / "tools" / "git-hooks" / "check_staged.py")],
        cwd=repo, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120,
    )


def _stage(repo: Path, rel: str, content: str = "probe\n") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-f", rel], cwd=repo, check=True)


def test_guard_rejects_a_staged_retired_closure_member(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _stage(repo, "app/static/app.js", "window.retired = 1;\n")
    result = _run_guard(repo)
    assert result.returncode != 0, (
        "guard accepted a staged retired-closure file; stdout=" + result.stdout
    )
    assert "RETIRED-8765" in result.stdout, result.stdout


def test_guard_rejects_the_retired_server_module(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _stage(repo, "scripts/quant_app_server.py", "PORT = 8765\n")
    result = _run_guard(repo)
    assert result.returncode != 0, result.stdout
    assert "RETIRED-8765" in result.stdout, result.stdout


def test_guard_accepts_an_ordinary_source_file(tmp_path: Path) -> None:
    """A guard that rejects everything is as useless as one that rejects nothing."""
    repo = _make_repo(tmp_path)
    _stage(repo, "quantdesk/ordinary_module.py", "VALUE = 1\n")
    result = _run_guard(repo)
    assert result.returncode == 0, (
        "guard rejected an ordinary source file; stdout=" + result.stdout
    )


def test_guard_rejects_runtime_state_paths(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _stage(repo, "state/ai_trader/ledger.json", "{}\n")
    result = _run_guard(repo)
    assert result.returncode != 0, result.stdout
    assert "runtime-path" in result.stdout, result.stdout


def test_guard_allows_deletions_of_retired_members(tmp_path: Path) -> None:
    """Shrinking is the goal: removing a retired file must never be blocked."""
    repo = _make_repo(tmp_path)
    _stage(repo, "app/static/app.js", "window.retired = 1;\n")
    # commit it with the guard bypassed, then stage its deletion
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-q",
                    "-m", "seed"], cwd=repo, check=True)
    subprocess.run(["git", "rm", "-q", "app/static/app.js"], cwd=repo, check=True)
    result = _run_guard(repo)
    assert result.returncode == 0, (
        "guard blocked the DELETION of a retired file; stdout=" + result.stdout
    )
