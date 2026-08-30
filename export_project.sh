#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON:-python3}"
"$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

root = Path.cwd()
dist = root / "dist"
stage = dist / "_quantdesk_clean_stage"
zip_path = dist / "quantdesk_clean.zip"

keep_files = {
    "README.md", "ROADMAP.md", "AGENTS.md", "requirements.txt", "requirements-dev.txt",
    "pyproject.toml", ".gitignore", "QuantDesk.cmd", "QuantDesk.vbs", "start_ui.cmd",
    "setup_windows.ps1", "run_tests.cmd",
    "run_daily_smoke.cmd", "self_check.cmd", "create_desktop_shortcuts.ps1",
    "create_desktop_shortcuts.cmd", "export_project.ps1", "export_project.cmd",
    "setup_linux.sh", "start_ui.sh", "run_tests.sh",
    "self_check.sh", "export_project.sh",
}
keep_dirs = {"ai_trader", "quantdesk", "scripts", "app", "config", "docs", "templates", "tests"}
excluded_dirs = {
    ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".vscode",
    ".idea", ".git", "external_refs", ".tmp_tests", ".quantdesk_test_tmp", "logs", "reports", "results", "state",
    "vectorbt", "backtrader", "qlib", "FinRL", "finrl", "node_modules",
}
excluded_files = {
    "config.local.yaml", "paper_portfolio.json", "watchlist.json",
    "manual_confirmation_queue.json", "live_monitor_events.jsonl", "Thumbs.db",
}
excluded_suffixes = {".pyc", ".log", ".tmp", ".bak", ".db", ".sqlite", ".parquet", ".pkl", ".pickle", ".zip", ".7z"}
excluded_prefixes = {
    Path("data/raw"),
    Path("data/daily"),
    Path("data/news"),
    Path("data/cache"),
    Path("data/universe"),
    Path("data/features"),
    Path("data/intraday"),
    Path("data/test_runtime"),
    Path("data/chanlun/runtime"),
    Path("data/chanlun/fractals"),
    Path("data/chanlun/bi"),
    Path("data/chanlun/segments"),
    Path("data/knowledge/sources"),
    Path("data/knowledge/raw_text"),
    Path("data/knowledge/fulltext"),
}

def is_excluded(path: Path) -> bool:
    rel = path.relative_to(root)
    if any(part in excluded_dirs for part in rel.parts):
        return True
    if path.name in excluded_files or path.suffix in excluded_suffixes:
        return True
    return any(rel == prefix or prefix in rel.parents for prefix in excluded_prefixes)

def copy_file(path: Path) -> None:
    if is_excluded(path):
        return
    target = stage / path.relative_to(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)

dist.mkdir(exist_ok=True)
if stage.exists():
    shutil.rmtree(stage)
stage.mkdir(parents=True)

for name in keep_files:
    path = root / name
    if path.is_file():
        copy_file(path)
for name in keep_dirs:
    base = root / name
    if base.is_dir():
        for path in base.rglob("*"):
            if path.is_file():
                copy_file(path)

for path in stage.rglob("*"):
    if path.suffix in {".py", ".json"} and path.is_file():
        text = path.read_text(encoding="utf-8", errors="ignore")
        sanitized = re.sub(r"C:\\Users\\\S+", "<USER_HOME>", text)
        sanitized = re.sub(r"/home/\S+", "<USER_HOME>", sanitized)
        if sanitized != text:
            path.write_text(sanitized, encoding="utf-8")

if zip_path.exists():
    zip_path.unlink()
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in stage.rglob("*"):
        if path.is_file():
            archive.write(path, path.relative_to(stage).as_posix())
shutil.rmtree(stage)
print(f"Clean project exported to: {zip_path}")
print("Runtime state and personal data are excluded from the clean zip.")
PY
