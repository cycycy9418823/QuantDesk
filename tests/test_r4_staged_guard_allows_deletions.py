"""Staging the DELETION of a blocked path must not be blocked.

Why this exists: the guard reads `git diff --cached --name-only`, which discards
the status letter, so a staged deletion is indistinguishable from a staged
addition. Removing config/llm_config.json from tracking - the correct fix for a
file that can hold an API key - therefore turned self_check red, and the only way
to get green was to un-stage the very cleanup the guard exists to encourage.

The pre-commit hook already gets this right (`if code.startswith("D"): continue`),
so this is the guard disagreeing with its own sibling. Deleting a file cannot
introduce data into the repository; shrinking is always allowed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk import repo_hygiene  # noqa: E402


BLOCKED_PATH = "config/llm_config.json"
RUNTIME_PATH = "data/daily/000001.csv"


def test_plain_path_list_still_blocks_an_added_runtime_path() -> None:
    """Backwards compatibility: a bare path list keeps its old meaning."""
    result = repo_hygiene.validate_staged_source_handoff([RUNTIME_PATH])
    assert result["ok"] is False
    assert RUNTIME_PATH in result["blocked_paths"]


def test_staged_deletion_of_a_blocked_path_is_allowed() -> None:
    """A deletion can only ever shrink the tree, so it must pass."""
    result = repo_hygiene.validate_staged_source_handoff([f"D\t{BLOCKED_PATH}"])
    assert result["ok"] is True, (
        f"staging the deletion of {BLOCKED_PATH} was blocked; "
        f"blocked_paths={result['blocked_paths']}"
    )
    assert result["blocked_paths"] == []


def test_staged_deletion_of_a_runtime_path_is_allowed() -> None:
    result = repo_hygiene.validate_staged_source_handoff([f"D\t{RUNTIME_PATH}"])
    assert result["ok"] is True, result["blocked_paths"]


def test_status_aware_addition_of_a_runtime_path_is_still_blocked() -> None:
    """Accepting status codes must not become a way to smuggle data in."""
    for code in ("A", "M", "R100", "C75"):
        result = repo_hygiene.validate_staged_source_handoff([f"{code}\t{RUNTIME_PATH}"])
        assert result["ok"] is False, f"code {code} let a runtime path through"
        assert RUNTIME_PATH in result["blocked_paths"]


def test_mixed_batch_reports_only_the_added_offender() -> None:
    result = repo_hygiene.validate_staged_source_handoff([
        f"D\t{BLOCKED_PATH}",
        "M\tquantdesk/backtest.py",
        f"A\t{RUNTIME_PATH}",
    ])
    assert result["ok"] is False
    assert result["blocked_paths"] == [RUNTIME_PATH], result["blocked_paths"]


def test_rename_out_of_a_blocked_path_uses_the_destination() -> None:
    """git reports renames as `R100<TAB>old<TAB>new`; the destination decides.

    The destination is what will exist after the commit, so it is the only field
    that can say whether data is entering the tree. `config/` deliberately is not
    an allowed category - it falls to unknown_category_requires_review - so a
    genuinely allowed destination is used here instead.
    """
    result = repo_hygiene.validate_staged_source_handoff([
        f"R100\t{RUNTIME_PATH}\tquantdesk/rescued_module.py"
    ])
    assert result["ok"] is True, (
        "renaming a runtime path out to a source path was blocked; "
        f"blocked={result['blocked_paths']}"
    )

    result = repo_hygiene.validate_staged_source_handoff([
        f"R100\tquantdesk/backtest.py\t{RUNTIME_PATH}"
    ])
    assert result["ok"] is False, "a rename INTO a runtime path must be blocked"
    assert RUNTIME_PATH in result["blocked_paths"]


def test_self_check_collects_status_codes_not_bare_names() -> None:
    """The caller must ask git for status, or the guard can never see a deletion."""
    source = (ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8", errors="replace")
    fn = source.split("def check_staged_source_handoff_guard(")[1].split("\ndef ")[0]
    assert '"--name-status"' in fn, (
        "check_staged_source_handoff_guard still uses --name-only, so a staged "
        "deletion is indistinguishable from a staged addition"
    )
    assert '"--name-only"' not in fn


def test_guard_agrees_with_the_pre_commit_hook_on_deletions() -> None:
    """Both gates must treat deletions the same way, or they contradict."""
    hook = (ROOT / "tools" / "git-hooks" / "check_staged.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert 'code.startswith("D")' in hook, "the hook no longer skips deletions"
    result = repo_hygiene.validate_staged_source_handoff([f"D\t{RUNTIME_PATH}"])
    assert result["ok"] is True, (
        "the hook allows deleting a runtime path but the guard blocks it"
    )


def test_live_repository_guard_is_green() -> None:
    """End to end against the real index, whatever is staged right now."""
    completed = subprocess.run(
        ["git", "diff", "--cached", "--name-status"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    entries = [l.strip() for l in (completed.stdout or "").splitlines() if l.strip()]
    result = repo_hygiene.validate_staged_source_handoff(entries)
    assert result["ok"] is True, (
        "the live index is blocked by the handoff guard: "
        f"{result['blocked_paths']}"
    )
