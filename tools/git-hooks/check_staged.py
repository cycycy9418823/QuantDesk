"""Pre-commit guard for the clean QuantDesk repository.

Installed by tools/git-hooks/install.cmd into .git/hooks/pre-commit.

This repo was built by pulling the product closure forward out of the legacy
tree. Two distinct disaster classes must never re-enter it:

1. RETIREMENT CLOSURE (this repo's specific risk).
   The retired port-8765 stack was deliberately left behind. During the
   migration an automated backfill script twice pulled members of it back in by
   following import/reference graphs, because a reference from a *dead*
   self_check function still looks like a real dependency. That is a silent
   regression: nothing turns red, the retired stack simply reappears. This
   guard makes it loud instead.

2. REPO BLOAT / RUNTIME LEAKAGE (inherited from the legacy tree, which reached
   4.18 GiB because a 1.45 GB CSV was committed twice).
   Big blobs and runtime/state paths must stay out of history. data/ and state/
   are junctions to the legacy tree; results/, reports/ and logs/ are generated.

Deletions (D) are always allowed - shrinking is the goal.
Escape hatch (rare, deliberate, single commit):
   set QUANTDESK_ALLOW_STAGED_OVERRIDE=1
Never leave it exported.

Exit codes: 0 = clean, 1 = blocked (commit aborted), 2 = hook crashed
(fails closed on purpose).
"""

from __future__ import annotations

import os
import subprocess
import sys

MAX_STAGED_MB = float(os.environ.get("QUANTDESK_MAX_STAGED_MB", "50"))

# --- class 1: the retired port-8765 closure. Exact paths, never fuzzy. -------
RETIRED_CLOSURE = {
    "scripts/quant_app_server.py",
    "scripts/live_monitor.py",
    "scripts/generate_sample_report.py",
    "quantdesk/server_payloads.py",
    "quantdesk/live_monitor.py",
    "app/index.html",
    "app/agent_chat.html",
    "app/static/app.js",
    "app/static/styles.css",
    "run_report_sample.cmd",
    "run_ui_sample.cmd",
    "run_ui_sample.sh",
}

# Legacy test closures that travel with the retired stack.
RETIRED_TEST_PREFIXES = (
    "tests/test_legacy_8765_",
)
RETIRED_TESTS = {
    "tests/test_api_state_safety.py",
}

# --- class 2: runtime / bloat -----------------------------------------------
BLOCKED_PATTERNS = [
    "data/",
    "state/",
    "results/",
    "reports/",
    "logs/",
    "dist/",
    "test_runtime/",
    "__pycache__/",
    ".venv/",
    ".quantdesk_test_tmp/",
]
BLOCKED_SUFFIXES = (".db", ".db-wal", ".db-shm", ".zip", ".bundle", ".parquet", ".duckdb")


def _run_git(*args: str) -> str:
    out = subprocess.run(["git", *args], capture_output=True)
    return (out.stdout or b"").decode("utf-8", "replace")


def main() -> int:
    if os.environ.get("QUANTDESK_ALLOW_STAGED_OVERRIDE") == "1":
        print("[pre-commit] override active; guard skipped this commit.")
        return 0

    blocked: list[str] = []
    lines = [l for l in _run_git("diff", "--cached", "--name-status", "-z").split("\0") if l]

    i = 0
    while i < len(lines):
        code = lines[i].strip()
        path = lines[i + 1] if i + 1 < len(lines) else ""
        i += 2
        if not path or not code or code.startswith("D"):
            continue
        posix = path.replace("\\", "/")

        # class 1 - retired closure must never come back
        if posix in RETIRED_CLOSURE:
            blocked.append(f"RETIRED-8765-CLOSURE {code} {posix}")
            continue
        if posix in RETIRED_TESTS or posix.startswith(RETIRED_TEST_PREFIXES):
            blocked.append(f"RETIRED-8765-TEST     {code} {posix}")
            continue

        # class 2 - runtime paths and artifact types
        if any(posix.startswith(p) or f"/{p}" in posix for p in BLOCKED_PATTERNS):
            blocked.append(f"runtime-path         {code} {posix}")
            continue
        if posix.startswith("_tmp") or posix.startswith("agent_workspace/"):
            blocked.append(f"scratch-path         {code} {posix}")
            continue
        if posix.endswith(BLOCKED_SUFFIXES):
            blocked.append(f"artifact-type        {code} {posix}")
            continue

        # class 2 - big blobs
        try:
            raw = subprocess.run(["git", "cat-file", "-s", f":{path}"], capture_output=True)
            size_mb = int((raw.stdout or b"0").decode().strip() or 0) / (1024 * 1024)
        except Exception:
            size_mb = MAX_STAGED_MB + 1  # unreadable index entry -> fail closed
        if size_mb >= MAX_STAGED_MB:
            blocked.append(f"big-blob {size_mb:.1f}MB >= {MAX_STAGED_MB}MB {posix}")

    if blocked:
        print("[pre-commit] BLOCKED - these staged changes violate repo policy:")
        for b in blocked[:40]:
            print(f"  - {b}")
        more = len(blocked) - 40
        if more > 0:
            print(f"  ...and {more} more")
        print()
        print("[pre-commit] RETIRED-8765-* means a file from the retired port-8765")
        print("             stack is being re-added. That stack was left behind on")
        print("             purpose; re-adding it silently undoes the migration.")
        print("[pre-commit] fix: unstage with 'git restore --staged <file>'.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
