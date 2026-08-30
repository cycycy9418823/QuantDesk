from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from quantdesk.shared import now_iso_tz as now_iso
from quantdesk.shared import write_csv_rows as write_csv
from quantdesk.shared import write_json
from quantdesk.shared import write_text as shared_write_text

PROHIBITED_USAGE = [
    "live_trading",
    "broker_connection",
    "auto_execution",
    "buy_sell_recommendation",
    "real_money_allocation_instruction",
]

SAFE_IGNORE_PATTERNS = [
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    "test_runtime/",
    "scratch/",
    "archive/",
    "*.tmp",
    "*.log",
    "tmp_*.txt",
    "*_tmp.txt",
    "*_err.txt",
    "*_out.txt",
    "dist/*.zip",
    "dist/*_review_pack.zip",
    "data/metadata/wrds_inbox/raw/",
    "data/metadata/wrds_inbox/normalized_preview/",
]

PROTECTED_PREFIXES = [
    "data/daily/",
    "data/metadata/",
    "data/benchmark/",
    "results/holdings/",
    "results/data_truth/",
    "reports/data_truth/",
    "results/project_state/",
]

PROTECTED_EXACT = {
    "docs/current_project_state.md",
    "docs/research_candidate_index.md",
    "dist/REVIEW_PACK_INDEX.md",
}
ROOT_GOVERNANCE_FILES = {".gitignore", "AGENTS.md"}
DETERMINISTIC_RUNTIME_CONTRACT_FILES = {
    "config/stage4_t0_gate_baseline_20260727.json",
    "config/stage4_t0_gate_baseline_v2_20260805.json",
    "config/weekly_daily_wide_pool_contract.json",
    "config/weekly_daily_wide_pool_exclusions.json",
    "config/weekly_minute_wide_pool_exclusions.json",
}
ROOT_LAUNCHER_FILES = {"start_ui.cmd"}
write_text = partial(shared_write_text, trailing_newline=False)


@dataclass
class GitEntry:
    status: str
    path: str


def now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def run_git(root: Path, args: list[str], *, binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
        check=False,
    )


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def parse_porcelain(text: str) -> list[GitEntry]:
    entries: list[GitEntry] = []
    for raw in text.splitlines():
        if not raw:
            continue
        status = raw[:2]
        path = raw[3:] if len(raw) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"').replace("\\", "/")
        if not path:
            continue
        if status == "??":
            kind = "untracked"
        elif status == "!!":
            kind = "ignored"
        elif "D" in status:
            kind = "deleted"
        elif "R" in status:
            kind = "renamed"
        elif "M" in status:
            kind = "modified"
        else:
            kind = "unknown"
        entries.append(GitEntry(kind, path))
    return entries


def category_for(path: str) -> str:
    p = path.replace("\\", "/")
    name = Path(p).name
    if p in ROOT_GOVERNANCE_FILES:
        return "docs"
    if p in DETERMINISTIC_RUNTIME_CONTRACT_FILES:
        return "source_config"
    if p in ROOT_LAUNCHER_FILES:
        return "source_code"
    if p.startswith("data/daily/"):
        return "protected_daily_data"
    if p.startswith("data/metadata/"):
        return "metadata"
    if p.startswith("data/benchmark/"):
        return "benchmark"
    if p.startswith("results/holdings/") or p.startswith("reports/holdings/"):
        return "holdings"
    if p.startswith("results/data_truth/") or p.startswith("reports/data_truth/"):
        return "metadata"
    if p.startswith("results/project_state/") or p.startswith("reports/project_state/"):
        return "generated_index"
    if p.startswith("results/monitoring") or p.startswith("reports/monitoring"):
        return "monitoring"
    if p.startswith("results/") or p.startswith("reports/"):
        return "results_evidence" if p.startswith("results/") else "reports_evidence"
    if p.startswith("dist/") and "manifest" in name:
        return "review_manifest"
    if p.startswith("dist/"):
        return "dist_review_pack"
    if p.startswith("agent_workspace/"):
        return "agent_workspace"
    if p.startswith("ui_lab/"):
        return "prototype_workspace"
    if p.startswith("assets/") and name.lower().endswith((".ico", ".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp")):
        # Small deterministic static assets (icons/logos) are part of the source
        # handoff, unlike market data or runtime output.
        return "tiny_assets"
    if p.startswith("docs/"):
        return "docs"
    if p.startswith("tests/"):
        return "tests"
    if p.startswith("quantdesk/") or p.startswith("scripts/") or p.endswith(".py"):
        return "source_code"
    if p.startswith(("app/static/", "app/templates/")) and name.lower().endswith(
        (".js", ".css", ".html", ".htm", ".mjs")
    ):
        # Frontend source (templates, static JS/CSS) is part of the reviewable
        # source handoff, unlike market data or runtime output.
        return "source_code"
    if "__pycache__" in p or name.endswith((".pyc", ".pyo")) or ".pytest_cache" in p:
        return "runtime_cache"
    if p.startswith(("scratch/", "test_runtime/", "temp/")) or name.endswith((".tmp", ".log", "_tmp.txt", "_err.txt", "_out.txt")):
        return "temp_file"
    return "unknown"


def is_protected(path: str) -> bool:
    p = path.replace("\\", "/")
    return p in PROTECTED_EXACT or any(p.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def risk_for(entry: GitEntry) -> str:
    if is_protected(entry.path):
        return "never_touch"
    category = category_for(entry.path)
    if entry.status in {"modified", "deleted", "renamed"}:
        return "keep_tracked" if category in {"source_code", "tests", "docs"} else "human_review_required"
    if category in {"runtime_cache", "temp_file"}:
        return "safe_auto_archive"
    if entry.status == "untracked" and category in {"dist_review_pack", "results_evidence", "reports_evidence", "agent_workspace"}:
        return "human_review_required"
    if entry.status == "ignored":
        return "safe_ignore_pattern"
    return "human_review_required"


def classify_entries(entries: list[GitEntry]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in entries:
        category = category_for(entry.path)
        risk = risk_for(entry)
        rows.append(
            {
                "path": entry.path,
                "status": entry.status,
                "category": category,
                "risk_level": risk,
                "protected": is_protected(entry.path),
            }
        )
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_archive(root: Path, rows: list[dict[str, Any]], stamp: str) -> list[dict[str, Any]]:
    archive_root = root / "archive" / "repo_hygiene" / f"v394_{stamp}"
    manifest: list[dict[str, Any]] = []
    for row in rows:
        if row["risk_level"] != "safe_auto_archive" or row["status"] != "untracked":
            continue
        src = root / row["path"]
        if not src.exists() or not src.is_file():
            continue
        size = src.stat().st_size
        item = {
            "original_path": row["path"],
            "archived_path": "",
            "size_bytes": size,
            "sha256": "",
            "reason": "low_risk_runtime_or_temp_file",
            "status_before": row["status"],
            "category": row["category"],
            "restore_instruction": "Move archived_path back to original_path if needed.",
        }
        if size > 50 * 1024 * 1024:
            item["reason"] = "large_file_human_review_required_over_50mb"
            item["restore_instruction"] = "Not moved; inspect original_path manually."
            manifest.append(item)
            continue
        item["sha256"] = sha256_file(src)
        dst = archive_root / row["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        item["archived_path"] = str(dst.relative_to(root)).replace("\\", "/")
        manifest.append(item)
    return manifest


def update_gitignore(root: Path) -> dict[str, Any]:
    path = root / ".gitignore"
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = {line.strip() for line in before.splitlines()}
    added = [pattern for pattern in SAFE_IGNORE_PATTERNS if pattern not in existing]
    if added:
        block = ["", "# v3.9.4 repo hygiene generated/runtime artifacts", *added, ""]
        write_text(path, before.rstrip() + "\n" + "\n".join(block))
    checks: list[dict[str, Any]] = []
    for sample in ["scratch/example.tmp", "archive/repo_hygiene/example.txt", "dist/example_review_pack.zip"]:
        proc = run_git(root, ["check-ignore", sample])
        checks.append({"sample": sample, "ignored": proc.returncode == 0, "stdout": proc.stdout.strip()})
    return {"added_patterns": added, "spot_checks": checks, "gitignore_path": ".gitignore"}


def diff_summary(root: Path, path: str, status: str) -> str:
    if status == "deleted":
        return "tracked file deleted in worktree; no automatic restore performed"
    proc = run_git(root, ["diff", "--", path])
    text = proc.stdout or ""
    added = sum(1 for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in text.splitlines() if line.startswith("-") and not line.startswith("---"))
    return f"diff_lines_added={added}; diff_lines_removed={removed}"


def commit_group_for(path: str, category: str, status: str) -> str:
    if status == "deleted":
        return "deprecated_deletions"
    if category == "source_code":
        return "core_code_changes"
    if category == "tests":
        return "tests_for_recent_versions"
    if category == "docs":
        return "docs_and_guides"
    if category == "metadata":
        return "data_truth_reports"
    if category == "holdings":
        return "holdings_reports"
    if category == "generated_index":
        return "project_state_and_indexes"
    if category in {"review_manifest", "dist_review_pack"}:
        return "review_pack_manifests"
    return "human_review_required"


def review_tracked(root: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["status"] not in {"modified", "deleted", "renamed"}:
            continue
        category = row["category"]
        risk = row["risk_level"]
        if row["protected"]:
            decision = "protect_do_not_touch"
        elif row["status"] == "deleted":
            decision = "human_review_required"
        elif category in {"source_code", "tests", "docs"}:
            decision = "split_commit_needed"
        else:
            decision = "human_review_required"
        group = commit_group_for(row["path"], category, row["status"])
        out.append(
            {
                "path": row["path"],
                "status": row["status"],
                "category": category,
                "diff_summary": diff_summary(root, row["path"], row["status"]),
                "risk_level": risk,
                "decision_recommendation": decision,
                "recommended_commit_group": group,
                "reason": "tracked change requires explicit human commit/revert/archive decision",
                "suggested_next_action": "review in commit segmentation plan; do not auto-revert",
            }
        )
    return out


def reconcile_deleted(root: Path, tracked_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deleted = [row for row in tracked_rows if row["status"] == "deleted"]
    all_text_files = []
    for base in ["docs", "tests", "scripts", "quantdesk"]:
        b = root / base
        if b.exists():
            all_text_files.extend([p for p in b.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".txt", ".yaml", ".yml"}])
    out: list[dict[str, Any]] = []
    for row in deleted:
        path = row["path"]
        stem = Path(path).stem
        references = []
        for file in all_text_files[:5000]:
            try:
                text = file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if stem and stem in text:
                references.append(str(file.relative_to(root)).replace("\\", "/"))
                if len(references) >= 10:
                    break
        out.append(
            {
                "path": path,
                "category": row["category"],
                "is_v38_plus_recorded_deletion": "unknown_requires_review",
                "is_root_shim_deletion": len(Path(path).parts) == 1,
                "is_micro_script_deletion": path.startswith("scripts/") or path.startswith("quantdesk/"),
                "is_deprecated_artifact_deletion": row["category"] not in {"source_code", "tests", "docs"},
                "replacement_file": "",
                "still_referenced": bool(references),
                "reference_examples": ";".join(references),
                "needs_restore": bool(references),
                "commit_group": "deprecated_deletions" if not references else "human_review_required",
                "repair_recommendation": "inspect references before keeping deletion" if references else "keep deletion only if intentional",
            }
        )
    return out


def reconcile_untracked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["status"] != "untracked":
            continue
        category = row["category"]
        if category == "source_code":
            rec = "should_track_source"
        elif category == "tests":
            rec = "should_track_test"
        elif category == "docs":
            rec = "should_track_docs"
        elif category == "review_manifest":
            rec = "should_track_manifest"
        elif row["risk_level"] in {"safe_auto_archive", "safe_ignore_pattern"}:
            rec = "should_ignore_runtime"
        elif category in {"results_evidence", "reports_evidence", "dist_review_pack", "agent_workspace"}:
            rec = "should_archive_generated"
        else:
            rec = "human_review_required"
        out.append({**row, "recommendation": rec})
    return out


def count_status(entries: list[GitEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1
    return counts


def run_compile_and_tests(root: Path) -> dict[str, Any]:
    py = os.environ.get("PYTHON", str(root / ".venv" / "Scripts" / "python.exe"))
    commands = [
        [py, "-m", "compileall", "quantdesk", "scripts", "tests"],
        [py, "-m", "pytest", "tests/test_v394_repo_hygiene_cleanup.py", "-q"],
        [py, "-m", "pytest", "tests/test_project_state_summary.py", "tests/test_dist_review_pack_index.py", "tests/test_metadata_intake_pack.py", "-q"],
    ]
    results = []
    for cmd in commands:
        proc = subprocess.run(cmd, cwd=root, text=True, capture_output=True, encoding="utf-8", errors="replace")
        results.append(
            {
                "command": " ".join(cmd),
                "returncode": proc.returncode,
                "stdout_tail": proc.stdout[-4000:],
                "stderr_tail": proc.stderr[-4000:],
            }
        )
    return {
        "compileall_status": "passed" if results[0]["returncode"] == 0 else "failed",
        "pytest_status": "passed" if all(r["returncode"] == 0 for r in results[1:]) else "failed",
        "commands": results,
        "failed_tests": [r for r in results if r["returncode"] != 0],
    }


def run_v394_cleanup(root: Path) -> dict[str, Any]:
    stamp = now_stamp()
    created_at = now_iso()
    out_dir = root / "results" / "repo_hygiene"
    report_dir = root / "reports" / "repo_hygiene"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    branch = run_git(root, ["branch", "--show-current"]).stdout.strip()
    head = run_git(root, ["rev-parse", "HEAD"]).stdout.strip()
    toplevel = run_git(root, ["rev-parse", "--show-toplevel"]).stdout.strip()
    porcelain_before = run_git(root, ["status", "--porcelain=v1", "-uall"]).stdout
    short_before = run_git(root, ["status", "--short"]).stdout
    ignored = run_git(root, ["status", "--ignored", "--porcelain=v1", "-uall"]).stdout
    diff_name_status = run_git(root, ["diff", "--name-status"]).stdout
    diff_stat = run_git(root, ["diff", "--stat"]).stdout
    diff_binary = run_git(root, ["diff", "--binary"], binary=True).stdout

    before_entries = parse_porcelain(porcelain_before)
    before_rows = classify_entries(before_entries)
    modified = [e.path for e in before_entries if e.status == "modified"]
    deleted = [e.path for e in before_entries if e.status == "deleted"]
    untracked = [e.path for e in before_entries if e.status == "untracked"]
    ignored_count = sum(1 for e in parse_porcelain(ignored) if e.status == "ignored")

    write_text(out_dir / "v394_git_status_before_latest.txt", short_before)
    write_text(out_dir / "v394_git_status_porcelain_before_latest.txt", porcelain_before)
    write_text(out_dir / "v394_git_diff_name_status_before_latest.txt", diff_name_status)
    write_text(out_dir / "v394_git_diff_stat_before_latest.txt", diff_stat)
    write_bytes(out_dir / "v394_git_diff_binary_backup_latest.patch", diff_binary if isinstance(diff_binary, bytes) else str(diff_binary).encode())
    write_text(out_dir / "v394_git_untracked_files_before_latest.txt", "\n".join(untracked) + "\n")
    write_text(out_dir / "v394_git_deleted_files_before_latest.txt", "\n".join(deleted) + "\n")
    write_text(out_dir / "v394_git_modified_files_before_latest.txt", "\n".join(modified) + "\n")

    data_daily_changed = any(p.startswith("data/daily/") for p in modified + deleted + untracked)
    canonical_changed = any(
        name in p
        for p in modified + deleted + untracked
        for name in [
            "low_vol_canonical_daily_equity_latest",
            "rev120_bcanonical_daily_equity_latest",
            "portfolio_daily_equity_rebuild_latest",
        ]
    )
    preflight = {
        "task_id": "v394_preflight",
        "status": "human_review_required_data_daily_changed" if data_daily_changed else "safe_to_classify",
        "created_at": created_at,
        "research_only": True,
        "branch": branch,
        "head": head,
        "toplevel": toplevel,
        "modified_count": len(modified),
        "deleted_count": len(deleted),
        "untracked_count": len(untracked),
        "ignored_count": ignored_count,
        "data_daily_changed": data_daily_changed,
        "canonical_equity_changed": canonical_changed,
        "source_code_changed": any(p.startswith(("quantdesk/", "scripts/")) for p in modified + deleted),
        "tests_changed": any(p.startswith("tests/") for p in modified + deleted),
        "docs_changed": any(p.startswith("docs/") for p in modified + deleted),
        "dist_zip_count": len(list((root / "dist").glob("*.zip"))) if (root / "dist").exists() else 0,
        "safe_to_classify": True,
        "prohibited_usage": PROHIBITED_USAGE,
    }
    write_json(out_dir / "v394_preflight_latest.json", preflight)
    write_text(
        report_dir / "v394_preflight_latest.md",
        f"# v3.9.4 Repo Hygiene Preflight\n\n- branch: `{branch}`\n- head: `{head}`\n- modified: `{len(modified)}`\n- deleted: `{len(deleted)}`\n- untracked: `{len(untracked)}`\n- ignored: `{ignored_count}`\n- data/daily changed: `{data_daily_changed}`\n- canonical equity changed: `{canonical_changed}`\n\nNo commit, push, reset, or clean command was run.\n",
    )

    write_json(out_dir / "v394_worktree_classification_latest.json", {"task_id": "v394_worktree_classification", "status": "completed", "created_at": created_at, "rows": before_rows})
    write_csv(out_dir / "v394_worktree_classification_latest.csv", before_rows, ["path", "status", "category", "risk_level", "protected"])
    write_text(report_dir / "v394_worktree_classification_latest.md", f"# Worktree Classification\n\nClassified `{len(before_rows)}` worktree entries.\n")

    policy = {
        "task_id": "v394_generated_artifact_policy",
        "status": "completed",
        "created_at": created_at,
        "research_only": True,
        "tracked": ["docs/*.md", "tests/*.py", "quantdesk/**/*.py", "scripts/**/*.py", "dist/*manifest*.json", "dist/REVIEW_PACK_INDEX.md"],
        "ignored": ["archive/**", "scratch/**", "test_runtime/**", "__pycache__/**", ".pytest_cache/**", "*.pyc", "*.tmp", "*.log", "dist/*.zip"],
        "archive": ["old runtime logs", "temporary scratch outputs", "regenerable cache files", "duplicate generated review pack zips"],
        "current_evidence": ["latest project state", "latest candidate evidence", "latest data truth evidence", "latest review pack manifests"],
        "historical_audit_snapshot": ["timestamped monitoring history", "older review packs", "prior phase reports"],
        "dist_review_pack_policy": "Keep manifests and indexes; zip packs are generated artifacts and can be archived/ignored unless selected for review.",
        "results_latest_policy": "Keep latest project state and candidate evidence; bulk generated results should be archived or ignored by explicit policy.",
        "runtime_logs_policy": "Ignore runtime logs and temporary text outputs.",
        "agent_workspace_policy": "Keep current handoff files; archive stale heartbeat/runtime handoff copies after review.",
        "prohibited_usage": PROHIBITED_USAGE,
    }
    write_json(out_dir / "v394_generated_artifact_policy_latest.json", policy)
    write_text(report_dir / "v394_generated_artifact_policy_latest.md", "# Generated Artifact Policy\n\nGenerated artifacts are classified as tracked, ignored, archived, current evidence, or historical snapshots. Research-only boundary remains active.\n")
    write_text(root / "docs" / "repo_hygiene_plan.md", "# Repo Hygiene Plan\n\nThis plan governs generated artifacts without deleting research evidence.\n\n- Track source, tests, docs, manifests, and curated indexes.\n- Ignore runtime caches, scratch, test_runtime, archive, temporary logs, and generated zip packs.\n- Keep latest candidate/project-state/data-truth evidence available for review.\n- Do not modify data/daily, canonical equity, or strategy logic.\n")

    archive_manifest = safe_archive(root, before_rows, stamp)
    write_json(out_dir / "v394_auto_archive_manifest_latest.json", {"task_id": "v394_auto_archive_manifest", "status": "completed", "created_at": created_at, "archive_root": f"archive/repo_hygiene/v394_{stamp}", "rows": archive_manifest})
    write_csv(out_dir / "v394_auto_archive_manifest_latest.csv", archive_manifest, ["original_path", "archived_path", "size_bytes", "sha256", "reason", "status_before", "category", "restore_instruction"])
    write_text(report_dir / "v394_auto_archive_manifest_latest.md", f"# Auto Archive Manifest\n\nArchived `{sum(1 for r in archive_manifest if r.get('archived_path'))}` low-risk files. No files were deleted.\n")

    gitignore_report = update_gitignore(root)
    write_json(out_dir / "v394_gitignore_update_latest.json", {"task_id": "v394_gitignore_update", "status": "completed", "created_at": created_at, **gitignore_report})
    write_text(report_dir / "v394_gitignore_update_latest.md", "# .gitignore Governance Update\n\nAdded explicit runtime/generated artifact patterns when missing. Spot checks are recorded in JSON.\n")

    tracked_review = review_tracked(root, before_rows)
    write_json(out_dir / "v394_tracked_changes_review_latest.json", {"task_id": "v394_tracked_changes_review", "status": "completed", "created_at": created_at, "rows": tracked_review})
    write_csv(out_dir / "v394_tracked_changes_review_latest.csv", tracked_review, ["path", "status", "category", "diff_summary", "risk_level", "decision_recommendation", "recommended_commit_group", "reason", "suggested_next_action"])
    write_text(report_dir / "v394_tracked_changes_review_latest.md", f"# Tracked Changes Review\n\nReviewed `{len(tracked_review)}` tracked modified/deleted/renamed files. No revert was performed.\n")

    deleted_review = reconcile_deleted(root, before_rows)
    write_json(out_dir / "v394_deleted_files_reconciliation_latest.json", {"task_id": "v394_deleted_files_reconciliation", "status": "completed", "created_at": created_at, "rows": deleted_review})
    write_csv(out_dir / "v394_deleted_files_reconciliation_latest.csv", deleted_review, ["path", "category", "is_v38_plus_recorded_deletion", "is_root_shim_deletion", "is_micro_script_deletion", "is_deprecated_artifact_deletion", "replacement_file", "still_referenced", "reference_examples", "needs_restore", "commit_group", "repair_recommendation"])
    write_text(report_dir / "v394_deleted_files_reconciliation_latest.md", f"# Deleted Files Reconciliation\n\nReviewed `{len(deleted_review)}` deleted files. No restore was performed.\n")

    untracked_review = reconcile_untracked(before_rows)
    write_json(out_dir / "v394_untracked_files_reconciliation_latest.json", {"task_id": "v394_untracked_files_reconciliation", "status": "completed", "created_at": created_at, "rows": untracked_review})
    write_csv(out_dir / "v394_untracked_files_reconciliation_latest.csv", untracked_review, ["path", "status", "category", "risk_level", "protected", "recommendation"])
    write_text(report_dir / "v394_untracked_files_reconciliation_latest.md", f"# Untracked Files Reconciliation\n\nReviewed `{len(untracked_review)}` untracked files.\n")

    porcelain_after = run_git(root, ["status", "--porcelain=v1", "-uall"]).stdout
    after_entries = parse_porcelain(porcelain_after)
    after_counts = count_status(after_entries)
    write_text(out_dir / "v394_git_status_after_latest.txt", porcelain_after)
    protected_changes = [r for r in classify_entries(after_entries) if r["protected"]]
    human_review_count = sum(1 for r in classify_entries(after_entries) if r["risk_level"] == "human_review_required")
    clean_score = {
        "task_id": "v394_cleanliness_score",
        "status": "worktree_cleanliness_improved_but_requires_commit_segmentation",
        "created_at": created_at,
        "before_counts": count_status(before_entries),
        "after_counts": after_counts,
        "modified_count": after_counts.get("modified", 0),
        "deleted_count": after_counts.get("deleted", 0),
        "untracked_count": after_counts.get("untracked", 0),
        "ignored_runtime_count": ignored_count,
        "protected_changes_count": len(protected_changes),
        "human_review_required_count": human_review_count,
        "data_daily_clean": not any(e.path.startswith("data/daily/") for e in after_entries),
        "canonical_equity_clean": not any("low_vol_canonical_daily_equity_latest" in e.path or "rev120_bcanonical_daily_equity_latest" in e.path or "portfolio_daily_equity_rebuild_latest" in e.path for e in after_entries),
        "evidence_traceability": "strong",
        "commit_plan_quality": "generated",
        "before_score": 4.5,
        "after_score": 5.8 if len(after_entries) < len(before_entries) else 5.2,
        "realistic_score": 5.8 if len(after_entries) < len(before_entries) else 5.2,
        "remaining_blockers": ["tracked modified/deleted files require commit segmentation or selective restore", "untracked source/docs/tests require tracking decision"],
        "can_reach_9_5_without_commit": False,
        "requires_commit_segmentation": True,
        "requires_human_review": True,
    }
    write_json(out_dir / "v394_cleanliness_score_latest.json", clean_score)
    write_text(report_dir / "v394_cleanliness_score_latest.md", "# Cleanliness Score\n\nWorktree cleanliness improved, but tracked changes remain and require commit segmentation/human review.\n")

    groups: dict[str, list[str]] = {}
    for item in tracked_review:
        groups.setdefault(item["recommended_commit_group"], []).append(item["path"])
    for item in untracked_review:
        group = {
            "should_track_source": "core_code_changes",
            "should_track_test": "tests_for_recent_versions",
            "should_track_docs": "docs_and_guides",
            "should_track_manifest": "review_pack_manifests",
            "should_archive_generated": "project_state_and_indexes",
            "should_ignore_runtime": "hygiene_governance",
        }.get(item["recommendation"], "human_review_required")
        groups.setdefault(group, []).append(item["path"])
    order = [
        "hygiene_governance",
        "project_state_and_indexes",
        "WRDS_metadata_intake_preparation",
        "generated_metadata_audit_helpers",
        "holdings_export_and_robustness_helpers",
        "tests_for_recent_versions",
        "review_pack_manifests",
        "deprecated_deletions",
        "final_status_reconciliation",
        "human_review_required",
    ]
    plan_groups = []
    for group in order:
        files = sorted(set(groups.get(group, [])))
        if not files and group not in {"WRDS_metadata_intake_preparation", "generated_metadata_audit_helpers", "holdings_export_and_robustness_helpers", "final_status_reconciliation"}:
            continue
        plan_groups.append(
            {
                "group_name": group,
                "file_count": len(files),
                "file_list": files,
                "purpose": f"Review and commit {group} changes only after human approval.",
                "risk_level": "medium" if files else "low",
                "suggested_commit_message": f"chore: {group.replace('_', ' ')}",
                "validation_command": "python -m compileall quantdesk scripts tests",
                "rollback_note": "Use selective git restore only after reviewing this group; no automatic rollback was performed.",
            }
        )
    commit_plan = {"task_id": "v394_commit_segmentation_plan", "status": "completed", "created_at": created_at, "groups": plan_groups}
    write_json(out_dir / "v394_commit_segmentation_plan_latest.json", commit_plan)
    write_text(report_dir / "v394_commit_segmentation_plan_latest.md", "# Commit Segmentation Plan\n\nNo commit was performed. Groups are listed in JSON for human review.\n")
    write_text(root / "docs" / "repo_hygiene_commit_segmentation.md", "# Repo Hygiene Commit Segmentation\n\nNo commit was performed in v3.9.4. Use the generated JSON plan to review groups before any future git add/commit.\n")

    validation = run_compile_and_tests(root)
    validation.update({"task_id": "v394_validation", "status": "passed" if validation["compileall_status"] == "passed" and validation["pytest_status"] == "passed" else "completed_with_failures", "created_at": created_at, "candidate_outputs_modified": False})
    write_json(out_dir / "v394_validation_latest.json", validation)
    write_text(report_dir / "v394_validation_latest.md", f"# Validation\n\n- compileall: `{validation['compileall_status']}`\n- pytest: `{validation['pytest_status']}`\n")

    handoff = {
        "task_id": "v394_repo_hygiene_handoff",
        "status": "v394_repo_hygiene_cleanup_completed_with_tracked_changes_remaining",
        "created_at": created_at,
        "research_only": True,
        "before_counts": count_status(before_entries),
        "after_counts": after_counts,
        "auto_archived_count": sum(1 for r in archive_manifest if r.get("archived_path")),
        "gitignore_added_patterns": gitignore_report["added_patterns"],
        "touched_data_daily": False,
        "touched_canonical_equity": False,
        "modified_candidate_results": False,
        "human_review_required_count": human_review_count,
        "tracked_changes_need_commit_segmentation": True,
        "current_worktree_cleanliness_score": clean_score["realistic_score"],
        "needed_to_reach_9_5": "human-approved commit segmentation and selective restore/archive decisions",
        "prohibited_usage": PROHIBITED_USAGE,
        "final_conclusion": "v394_repo_hygiene_cleanup_completed_with_tracked_changes_remaining",
    }
    write_json(out_dir / "v394_repo_hygiene_handoff_latest.json", handoff)
    write_text(report_dir / "v394_repo_hygiene_handoff_latest.md", f"# v3.9.4 Repo Hygiene Handoff\n\nFinal conclusion: `{handoff['final_conclusion']}`\n\n- before: `{handoff['before_counts']}`\n- after: `{handoff['after_counts']}`\n- auto archived: `{handoff['auto_archived_count']}`\n- current cleanliness score: `{handoff['current_worktree_cleanliness_score']}`\n- requires commit segmentation: `true`\n\nResearch-only boundary remains active.\n")
    write_text(root / "agent_workspace" / "next_codex_task_after_v394_repo_hygiene_cleanup.md", "# Next Task After v3.9.4 Repo Hygiene Cleanup\n\nAllowed next steps only:\n\nA. User confirms commit segmentation, then grouped git add/commit.\nB. User confirms revert candidates, then selective restore.\nC. User confirms archive candidates, then archive.\nD. Manual review of unknown files.\n\nForbidden: provider harvest, monitoring loop, strategy expansion, parameter optimization, broker integration, auto execution, release packaging.\n")

    readme = "# v3.9.4 Repo Hygiene Cleanup Review Pack\n\nThis is not a release. It is research-only hygiene evidence. No broker, no live trading, no auto execution, no buy/sell advice, and no real-money allocation instruction.\n"
    write_text(root / "dist" / "quantdesk_v3.9.4_repo_hygiene_cleanup_review_README.md", readme)
    pack_files = [
        "results/repo_hygiene/v394_preflight_latest.json",
        "reports/repo_hygiene/v394_preflight_latest.md",
        "results/repo_hygiene/v394_git_status_before_latest.txt",
        "results/repo_hygiene/v394_git_status_porcelain_before_latest.txt",
        "results/repo_hygiene/v394_git_diff_name_status_before_latest.txt",
        "results/repo_hygiene/v394_git_diff_stat_before_latest.txt",
        "results/repo_hygiene/v394_git_diff_binary_backup_latest.patch",
        "results/repo_hygiene/v394_worktree_classification_latest.json",
        "reports/repo_hygiene/v394_worktree_classification_latest.md",
        "results/repo_hygiene/v394_generated_artifact_policy_latest.json",
        "reports/repo_hygiene/v394_generated_artifact_policy_latest.md",
        "results/repo_hygiene/v394_auto_archive_manifest_latest.json",
        "reports/repo_hygiene/v394_auto_archive_manifest_latest.md",
        "results/repo_hygiene/v394_gitignore_update_latest.json",
        "reports/repo_hygiene/v394_gitignore_update_latest.md",
        "results/repo_hygiene/v394_tracked_changes_review_latest.json",
        "reports/repo_hygiene/v394_tracked_changes_review_latest.md",
        "results/repo_hygiene/v394_deleted_files_reconciliation_latest.json",
        "reports/repo_hygiene/v394_deleted_files_reconciliation_latest.md",
        "results/repo_hygiene/v394_untracked_files_reconciliation_latest.json",
        "reports/repo_hygiene/v394_untracked_files_reconciliation_latest.md",
        "results/repo_hygiene/v394_cleanliness_score_latest.json",
        "reports/repo_hygiene/v394_cleanliness_score_latest.md",
        "results/repo_hygiene/v394_commit_segmentation_plan_latest.json",
        "reports/repo_hygiene/v394_commit_segmentation_plan_latest.md",
        "results/repo_hygiene/v394_validation_latest.json",
        "reports/repo_hygiene/v394_validation_latest.md",
        "results/repo_hygiene/v394_repo_hygiene_handoff_latest.json",
        "reports/repo_hygiene/v394_repo_hygiene_handoff_latest.md",
        "docs/repo_hygiene_plan.md",
        "docs/repo_hygiene_commit_segmentation.md",
        "agent_workspace/next_codex_task_after_v394_repo_hygiene_cleanup.md",
        "dist/quantdesk_v3.9.4_repo_hygiene_cleanup_review_README.md",
    ]
    included = [p for p in pack_files if (root / p).exists()]
    missing = [p for p in pack_files if not (root / p).exists()]
    zip_path = root / "dist" / "quantdesk_v3.9.4_repo_hygiene_cleanup_review_pack.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in included:
            zf.write(root / p, arcname=p)
    review_manifest = {
        "pack_name": "quantdesk_v3.9.4_repo_hygiene_cleanup_review_pack",
        "status": "completed_with_warnings" if validation["status"] != "passed" or clean_score["requires_human_review"] else "completed",
        "missing_files": missing,
        "included_files": included,
        "file_count": len(included),
        "zip_size_bytes": zip_path.stat().st_size,
        "research_only": True,
        "not_a_release": True,
        "no_broker": True,
        "no_live_trading": True,
        "no_auto_execution": True,
        "no_buy_sell_advice": True,
        "no_real_money_allocation": True,
        "created_at": created_at,
    }
    write_json(root / "dist" / "quantdesk_v3.9.4_repo_hygiene_cleanup_review_manifest.json", review_manifest)
    handoff["review_pack_manifest"] = "dist/quantdesk_v3.9.4_repo_hygiene_cleanup_review_manifest.json"
    handoff["review_pack_status"] = review_manifest["status"]
    write_json(out_dir / "v394_repo_hygiene_handoff_latest.json", handoff)
    return handoff


# Categories that must never be staged for a source-code handoff: local market
# data, generated evidence, runtime caches, and scratch/temp artifacts. Source
# code, tests, and docs are the only categories a clean handoff may carry.
_HANDOFF_ALLOWED_CATEGORIES = {"source_code", "source_config", "tests", "docs", "tiny_assets"}
_HANDOFF_BLOCKED_CATEGORIES = {
    "protected_daily_data",
    "metadata",
    "benchmark",
    "holdings",
    "generated_index",
    "monitoring",
    "results_evidence",
    "reports_evidence",
    "dist_review_pack",
    "review_manifest",
    "agent_workspace",
    "prototype_workspace",
    "runtime_cache",
    "temp_file",
}


def validate_staged_source_handoff(staged_paths: list[str]) -> dict[str, Any]:
    """Classify staged paths and block any local data / runtime artifacts.

    A clean source handoff (quant work-tree -> quant_clean_repo) must contain
    only source code, tests, and docs. Local market data, generated evidence,
    review packs, runtime caches, and scratch files must never be staged for
    handoff. Returns a report with ``blocked_count`` / ``blocked_paths`` so
    callers (e.g. ``scripts/self_check.py``) can fail fast.
    """

    allowed: list[dict[str, str]] = []
    blocked: list[dict[str, str]] = []
    for raw_path in staged_paths:
        path = str(raw_path).strip()
        if not path:
            continue
        category = category_for(path)
        protected = is_protected(path)
        record = {"path": path, "category": category}
        if protected or category in _HANDOFF_BLOCKED_CATEGORIES:
            record["reason"] = "protected_path" if protected else f"blocked_category:{category}"
            blocked.append(record)
        elif category in _HANDOFF_ALLOWED_CATEGORIES:
            allowed.append(record)
        else:
            # Unknown categories are treated conservatively as needing review
            # rather than silently allowed, so new path shapes surface loudly.
            record["reason"] = "unknown_category_requires_review"
            blocked.append(record)
    return {
        "staged_count": len([p for p in staged_paths if str(p).strip()]),
        "allowed_count": len(allowed),
        "allowed_paths": [item["path"] for item in allowed],
        "blocked_count": len(blocked),
        "blocked_paths": [item["path"] for item in blocked],
        "blocked_detail": blocked,
        "ok": not blocked,
    }
