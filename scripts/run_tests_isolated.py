"""Run pytest in a disposable source copy, with real runtime writes fenced off.

No real market data, ledgers, reports, secrets, or ignored local config are copied.
All test outputs remain below --output-root. No network source update is run.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_EXCLUDE = {"data", "state", "results", "reports", "logs", "dist", "attic", "test_runtime", ".venv", "node_modules", ".git", ".codex", ".pytest_cache", "__pycache__"}
_EXCLUDE_ANYWHERE = {".venv", "node_modules", ".git", ".codex", ".pytest_cache", "__pycache__"}


def build_test_copy(source: Path, output: Path, *, include_git_metadata: bool = True) -> tuple[Path, dict]:
    """Copy source and tiny tracked fixtures, preserving unstaged implementation."""
    source, output = source.resolve(), output.resolve()
    if output == source or source in output.parents:
        raise ValueError("output_root_must_be_outside_source_tree")
    output.mkdir(parents=True, exist_ok=True)
    sandbox = Path(tempfile.mkdtemp(prefix="qn-", dir=output))
    head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if include_git_metadata:
        # Clone only commit objects into the EMPTY disposable directory. Never
        # copy a worktree gitfile, alternates, credentials, hooks or hardlinks.
        subprocess.run(["git", "clone", "--quiet", "--no-local", "--no-hardlinks", "--no-checkout", "--depth", "1",
                        str(source), str(sandbox)], capture_output=True, check=True)
        cloned = subprocess.run(["git", "-C", str(sandbox), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        if cloned != head or not (sandbox / ".git").is_dir() or (sandbox / ".git" / "objects" / "info" / "alternates").exists():
            raise ValueError("test_git_metadata_not_independent_or_wrong_head")
        subprocess.run(["git", "-C", str(sandbox), "remote", "remove", "origin"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(sandbox), "read-tree", "HEAD"], capture_output=True, check=True)
    cp = subprocess.run(["git", "-C", str(source), "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture_output=True, check=True)
    names = sorted(set(cp.stdout.decode("utf-8").split("\0")) - {""})
    files: dict[str, str] = {}
    for name in names:
        relative = Path(name)
        if (relative.is_absolute() or ".." in relative.parts
                or any(part in _EXCLUDE_ANYWHERE for part in relative.parts)
                or (relative.parts and relative.parts[0] in _EXCLUDE)):
            continue
        if relative.name.startswith(".env") or relative.suffix.lower() in {".key", ".pem", ".pfx", ".zip", ".pyc"}:
            continue
        original = source / relative
        if not original.is_file() or original.is_symlink():
            continue
        destination = sandbox / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
        files[name] = hashlib.sha256(original.read_bytes()).hexdigest()
    manifest = {"source_root": str(source), "sandbox_root": str(sandbox), "base_commit": head,
                "created_at": datetime.now(timezone.utc).isoformat(), "files": files,
                "real_runtime_copied": False, "independent_git_metadata": include_git_metadata}
    (sandbox / ".quantdesk-test-sandbox.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return sandbox, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output-root", type=Path, default=Path(tempfile.gettempdir()) / "quantdesk-tests")
    parser.add_argument("--protect-root", action="append", type=Path, default=[])
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    sandbox, manifest = build_test_copy(args.source_root, args.output_root)
    guard = sandbox.parent / (sandbox.name + "-guard")
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "from pathlib import Path\nfrom quantdesk.maintenance.test_safety import configure_pytest_fence\n"
        "import os,sys\nsys.dont_write_bytecode=True\nconfigure_pytest_fence(Path(os.environ['QUANTDESK_TEST_SANDBOX']))\n", encoding="utf-8"
    )
    env = dict(os.environ)
    roots = [args.source_root.resolve(), *(root.resolve() for root in args.protect_root)]
    env.update(QUANTDESK_TEST_SANDBOX=str(sandbox), QUANTDESK_TEST_PROTECTED_ROOTS=os.pathsep.join(map(str, roots)), PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = os.pathsep.join([str(guard), str(sandbox), env.get("PYTHONPATH", "")])
    pytest_args = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    log = sandbox.parent / (sandbox.name + "-pytest.log")
    command = [sys.executable, "-X", "utf8", "-B", "-m", "pytest", *(pytest_args or ["tests", "-q"])]
    with log.open("w", encoding="utf-8") as handle:
        result = subprocess.run(command, cwd=sandbox, env=env, stdout=handle, stderr=subprocess.STDOUT)
    receipt = {"sandbox": str(sandbox), "source_root": manifest["source_root"], "base_commit": manifest["base_commit"],
               "source_file_count": len(manifest["files"]), "log": str(log), "command": command,
               "exit_code": result.returncode, "real_runtime_copied": False, "protected_roots": list(map(str, roots))}
    receipt_path = sandbox.parent / (sandbox.name + "-receipt.json")
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**receipt, "receipt": str(receipt_path)}, ensure_ascii=False, indent=2))
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
