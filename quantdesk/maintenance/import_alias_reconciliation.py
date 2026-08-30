from __future__ import annotations

import importlib
import json
import math
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CURRENT_STATUS_JSON = PROJECT_ROOT / "results" / "git_deletion_current_status_latest.json"
CURRENT_STATUS_MD = PROJECT_ROOT / "reports" / "git_deletion_current_status_latest.md"
ALIAS_JSON = PROJECT_ROOT / "results" / "import_alias_reconciliation_latest.json"
ALIAS_MD = PROJECT_ROOT / "reports" / "import_alias_reconciliation_latest.md"
REWRITE_JSON = PROJECT_ROOT / "results" / "import_rewrite_latest.json"
REWRITE_MD = PROJECT_ROOT / "reports" / "import_rewrite_latest.md"
REWRITE_MANIFEST_JSON = PROJECT_ROOT / "results" / "import_rewrite_manifest_latest.json"
REWRITE_MANIFEST_MD = PROJECT_ROOT / "reports" / "import_rewrite_manifest_latest.md"

RUNTIME_SCAN_ROOTS = ("quantdesk", "scripts", "tests")
DOC_SUFFIXES = {".md", ".txt", ".rst"}
SOURCE_SUFFIXES = {".py"}
DELETED_ROOT_RE = re.compile(r"^quantdesk/([^/]+)\.py$")


def _rel(path: str | Path) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def run_git_status_lines() -> list[str]:
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    lines = proc.stdout.splitlines()
    if proc.returncode != 0:
        lines.append(f"!! git_status_failed:{proc.stderr.strip()}")
    return lines


def parse_git_status(lines: list[str]) -> dict[str, list[str]]:
    buckets = {
        "deleted_tracked_files": [],
        "untracked_files": [],
        "modified_files": [],
        "renamed_files": [],
        "added_files": [],
        "other_files": [],
    }
    for line in lines:
        if not line:
            continue
        status = line[:2]
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        norm = path.replace("\\", "/")
        if status == "??":
            buckets["untracked_files"].append(norm)
        elif "D" in status:
            buckets["deleted_tracked_files"].append(norm)
        elif "R" in status:
            buckets["renamed_files"].append(norm)
        elif "A" in status:
            buckets["added_files"].append(norm)
        elif "M" in status:
            buckets["modified_files"].append(norm)
        else:
            buckets["other_files"].append(norm)
    return buckets


def deleted_module_names_from_paths(paths: list[str]) -> list[str]:
    names: list[str] = []
    for path in paths:
        match = DELETED_ROOT_RE.match(path)
        if match:
            names.append(match.group(1))
    return sorted(set(names))


def get_current_git_deletion_status() -> dict[str, Any]:
    parsed = parse_git_status(run_git_status_lines())
    deleted_module_names = deleted_module_names_from_paths(parsed["deleted_tracked_files"])
    non_deleted_candidates = [
        "factor_combiner",
        "combined_score_backtest",
        "point_in_time_universe",
        "factor_evaluation",
        "sector_rotation_engine",
    ]
    excluded = [name for name in non_deleted_candidates if name not in deleted_module_names]
    payload = {
        "ok": True,
        "status": "warning" if parsed["deleted_tracked_files"] else "passed",
        "task_type": "git_deletion_current_status",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "deleted_tracked_files": parsed["deleted_tracked_files"],
        "deleted_tracked_count": len(parsed["deleted_tracked_files"]),
        "untracked_count": len(parsed["untracked_files"]),
        "modified_count": len(parsed["modified_files"]),
        "deleted_module_names": deleted_module_names,
        "excluded_non_deleted_migration_candidates": excluded,
        "warning_if_scope_expanded": "Scope must stay limited to deleted_tracked_files only.",
        "warnings": ["deleted_tracked_files_present"] if parsed["deleted_tracked_files"] else [],
        "output_paths": {},
    }
    return payload


def _load_moved_modules() -> dict[str, str]:
    import quantdesk

    return dict(getattr(quantdesk, "_MOVED_MODULES", {}))


def inspect_shim_mechanism() -> dict[str, Any]:
    init_path = PROJECT_ROOT / "quantdesk" / "__init__.py"
    text = init_path.read_text(encoding="utf-8")
    return {
        "has_moved_modules": "_MOVED_MODULES" in text,
        "uses_getattr": "def __getattr__" in text,
        "uses_meta_path": "sys.meta_path" in text and "MetaPathFinder" in text,
        "uses_sys_modules_registration": "sys.modules" in text,
        "uses_reexport": "__all__" in text,
        "notes": ["legacy root modules are deprecated compatibility aliases"],
    }


def check_alias_coverage(
    deleted_module_names: list[str] | None = None,
    reference_scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    moved = _load_moved_modules()
    if deleted_module_names is None:
        deleted_module_names = get_current_git_deletion_status()["deleted_module_names"]
    reference_scan = reference_scan or scan_old_import_references(deleted_module_names)
    references_by_module = reference_scan.get("references_by_module") or {}
    import quantdesk

    covered: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    retired: list[dict[str, Any]] = []
    uncovered_details: list[dict[str, Any]] = []
    alias_ok = 0
    alias_fail = 0
    from_ok = 0
    from_fail = 0
    uncovered: list[str] = []
    for name in deleted_module_names:
        new_path = moved.get(name, "")
        new_exists = False
        alias_import_ok = False
        from_import_ok = False
        error = ""
        runtime_reference_count = int(references_by_module.get(name, 0) or 0)
        if new_path:
            try:
                importlib.import_module(new_path)
                new_exists = True
            except Exception as exc:
                error = f"new_module_import_failed:{type(exc).__name__}:{exc}"
            try:
                importlib.import_module(f"quantdesk.{name}")
                alias_import_ok = True
            except Exception as exc:
                error = error or f"alias_import_failed:{type(exc).__name__}:{exc}"
            try:
                getattr(quantdesk, name)
                from_import_ok = True
            except Exception as exc:
                error = error or f"from_import_failed:{type(exc).__name__}:{exc}"
            alias_ok += int(alias_import_ok)
            alias_fail += int(not alias_import_ok)
            from_ok += int(from_import_ok)
            from_fail += int(not from_import_ok)
        elif runtime_reference_count:
            uncovered.append(name)
            error = "missing_moved_module_mapping_with_runtime_import_refs"
        item = {
            "module_name": name,
            "old_import": f"quantdesk.{name}",
            "new_module_path": new_path,
            "new_module_exists": new_exists,
            "alias_import_ok": alias_import_ok,
            "from_import_ok": from_import_ok,
            "status": "covered" if new_exists and alias_import_ok and from_import_ok else "failed",
            "error": error,
            "runtime_reference_count": runtime_reference_count,
        }
        if new_path:
            (covered if item["status"] == "covered" else failed).append(item)
        elif runtime_reference_count:
            item["status"] = "uncovered_deleted_module"
            item["reason"] = "no_migration_target_but_runtime_imports_remain"
            uncovered_details.append(item)
        else:
            item["status"] = "retired_deleted"
            item["reason"] = "no_migration_target_and_no_runtime_imports"
            retired.append(item)
    warnings = []
    if failed:
        warnings.append("failed_aliases_present")
    if uncovered:
        warnings.append("uncovered_deleted_modules_present")
    if retired:
        warnings.append("retired_deleted_modules_present")
    recommendation = "all_deleted_modules_shim_covered"
    next_scope = "audit_logic_only"
    if failed:
        recommendation = "fix_broken_aliases_only"
        next_scope = "broken_alias_fix"
    elif uncovered:
        recommendation = "manual_review_required"
        next_scope = "manual_review"
    elif retired and covered:
        recommendation = "all_deleted_modules_resolved_or_retired"
        next_scope = "retired_legacy_cleanup"
    elif retired:
        recommendation = "all_deleted_modules_retired_or_unreferenced"
        next_scope = "retired_legacy_cleanup"
    return {
        "ok": not failed and not uncovered,
        "status": "passed" if not failed and not uncovered else "warning",
        "task_type": "import_alias_reconciliation",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "shim_mechanism": inspect_shim_mechanism(),
        "deleted_module_count": len(deleted_module_names),
        "alias_import_ok_count": alias_ok,
        "alias_import_fail_count": alias_fail,
        "from_import_ok_count": from_ok,
        "from_import_fail_count": from_fail,
        "alias_import_skipped_count": len(retired) + len(uncovered_details),
        "from_import_skipped_count": len(retired) + len(uncovered_details),
        "shim_covered_aliases": covered,
        "failed_aliases": failed,
        "uncovered_deleted_modules": uncovered,
        "uncovered_deleted_module_count": len(uncovered),
        "uncovered_deleted_module_details": uncovered_details,
        "retired_deleted_modules": [item["module_name"] for item in retired],
        "retired_deleted_module_count": len(retired),
        "retired_deleted_module_details": retired,
        "recommendation": recommendation,
        "next_phase_scope": next_scope,
        "warnings": warnings,
        "output_paths": {},
    }


def _iter_scan_files() -> list[Path]:
    files: list[Path] = []
    for root_name in RUNTIME_SCAN_ROOTS:
        root = PROJECT_ROOT / root_name
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            rel = _rel(path)
            if any(part in {"__pycache__", ".quantdesk_test_tmp"} for part in path.parts):
                continue
            if rel.startswith("quantdesk/maintenance/import_alias_reconciliation.py"):
                continue
            files.append(path)
    return files


def scan_old_import_references(deleted_module_names: list[str] | None = None) -> dict[str, Any]:
    if deleted_module_names is None:
        deleted_module_names = get_current_git_deletion_status()["deleted_module_names"]
    moved = _load_moved_modules()
    references: list[dict[str, Any]] = []
    shim_refs: list[dict[str, Any]] = []
    rewrite_candidates: list[dict[str, Any]] = []
    do_not_rewrite: list[dict[str, Any]] = []
    patterns = {
        name: [
            re.compile(rf"^\s*import\s+quantdesk\.{re.escape(name)}(?:\s+as\s+(\w+))?\s*$"),
            re.compile(rf"^\s*from\s+quantdesk\.{re.escape(name)}\s+import\s+(.+)$"),
            re.compile(rf"^\s*from\s+quantdesk\s+import\s+.*\b{re.escape(name)}\b"),
        ]
        for name in deleted_module_names
    }
    for path in _iter_scan_files():
        rel = _rel(path)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "quantdesk" not in text:
            continue
        candidate_names = [name for name in deleted_module_names if name in text]
        if not candidate_names:
            continue
        lines = text.splitlines()
        for lineno, line in enumerate(lines, 1):
            if "quantdesk" not in line:
                continue
            for name in candidate_names:
                compiled = patterns[name]
                if not any(pattern.search(line) for pattern in compiled):
                    continue
                item = {
                    "file": rel,
                    "line": lineno,
                    "module_name": name,
                    "old_import": line.strip(),
                    "new_module_path": moved.get(name, ""),
                }
                if rel == "quantdesk/__init__.py":
                    item["reason"] = "shim_definition_reference"
                    shim_refs.append(item)
                    do_not_rewrite.append(item)
                elif name in moved:
                    item["reason"] = "runtime_rewrite_candidate"
                    rewrite_candidates.append(item)
                else:
                    item["reason"] = "missing_mapping"
                    do_not_rewrite.append(item)
                references.append(item)
    refs_by_file: dict[str, int] = {}
    refs_by_module: dict[str, int] = {}
    for item in references:
        refs_by_file[item["file"]] = refs_by_file.get(item["file"], 0) + 1
        refs_by_module[item["module_name"]] = refs_by_module.get(item["module_name"], 0) + 1
    return {
        "old_import_references": references,
        "old_import_reference_count": len(references),
        "references_by_file": refs_by_file,
        "references_by_module": refs_by_module,
        "test_references": [item for item in references if item["file"].startswith("tests/")],
        "script_references": [item for item in references if item["file"].startswith("scripts/")],
        "package_references": [item for item in references if item["file"].startswith("quantdesk/") and item["file"] != "quantdesk/__init__.py"],
        "shim_definition_references": shim_refs,
        "rewrite_candidates": rewrite_candidates,
        "do_not_rewrite": do_not_rewrite,
    }


def _count_rewritten_imports_from_diff(deleted_module_names: list[str]) -> int:
    proc = subprocess.run(
        ["git", "diff", "-U0", "--", "quantdesk", "scripts", "tests"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="ignore",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode not in {0, 1}:
        return 0
    count = 0
    for line in proc.stdout.splitlines():
        if not line.startswith("-") or line.startswith("---"):
            continue
        text = line[1:].strip()
        for name in deleted_module_names:
            if (
                f"from quantdesk.{name} import" in text
                or f"import quantdesk.{name}" in text
                or re.search(rf"from\s+quantdesk\s+import\s+.*\b{re.escape(name)}\b", text)
            ):
                count += 1
                break
    return count


def build_import_rewrite_manifest(deleted_module_names: list[str] | None = None) -> dict[str, Any]:
    if deleted_module_names is None:
        deleted_module_names = get_current_git_deletion_status()["deleted_module_names"]
    moved = _load_moved_modules()
    proc = subprocess.run(
        ["git", "diff", "-U1", "--", "quantdesk", "scripts", "tests"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="ignore",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    entries: list[dict[str, Any]] = []
    current_file = ""
    pending_old: dict[str, Any] | None = None
    old_patterns = {
        name: re.compile(rf"(from\s+quantdesk\.{re.escape(name)}\s+import|import\s+quantdesk\.{re.escape(name)}|from\s+quantdesk\s+import\s+.*\b{re.escape(name)}\b)")
        for name in deleted_module_names
    }
    for raw in proc.stdout.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:].replace("\\", "/")
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            text = raw[1:].strip()
            for name, pattern in old_patterns.items():
                if pattern.search(text):
                    pending_old = {
                        "file": current_file,
                        "old_import": text,
                        "line_before": text,
                        "module_name": name,
                        "new_module_path": moved.get(name, ""),
                    }
                    break
            continue
        if raw.startswith("+") and not raw.startswith("+++") and pending_old:
            new_text = raw[1:].strip()
            module_name = pending_old["module_name"]
            if moved.get(module_name, "").replace(".", "/") in new_text.replace(".", "/") or moved.get(module_name, "") in new_text:
                entry = {
                    **pending_old,
                    "new_import": new_text,
                    "line_after": new_text,
                    "reason": "deleted_root_module_migrated_to_subpackage_alias",
                }
                entries.append(entry)
                pending_old = None
    core_files = {
        "quantdesk/research/strategy_viability_runner.py",
        "quantdesk/research/strategy_viability_report.py",
        "quantdesk/research/capital_lot_sensitivity.py",
        "quantdesk/research/combined_score_backtest.py",
        "quantdesk/research/factor_combiner.py",
        "scripts/run_strategy_viability_sprint.py",
        "scripts/run_capital_lot_sensitivity.py",
        "scripts/run_combined_score_backtest.py",
    }
    core_entries = [item for item in entries if item.get("file") in core_files]
    payload = {
        "ok": True,
        "status": "passed",
        "task_type": "import_rewrite_manifest",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "entry_count": len(entries),
        "entries": entries,
        "core_strategy_files_rewritten": sorted(set(item["file"] for item in core_entries)),
        "rollback_candidates": core_entries,
        "suspected_import_rewrite_root_cause": bool(core_entries),
        "warnings": ["core_strategy_file_import_rewrite_present"] if core_entries else [],
        "output_paths": {},
    }
    return payload


def build_import_rewrite_dry_run_payload(
    current: dict[str, Any] | None = None,
    scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = current or get_current_git_deletion_status()
    deleted_module_names = current["deleted_module_names"]
    scan = scan or scan_old_import_references(deleted_module_names)
    rewritten_count = _count_rewritten_imports_from_diff(deleted_module_names)
    before_count = scan["old_import_reference_count"] + rewritten_count
    payload = {
        "ok": True,
        "status": "passed" if not scan["rewrite_candidates"] else "warning",
        "task_type": "import_rewrite",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "old_import_references_before": before_count,
        "rewrite_candidates": scan["rewrite_candidates"],
        "rewritten_import_count": rewritten_count,
        "skipped_import_count": len(scan["rewrite_candidates"]),
        "old_import_references_after": scan["old_import_reference_count"],
        "remaining_legacy_imports": scan["old_import_references"],
        "remaining_legacy_import_count": scan["old_import_reference_count"],
        "remaining_reasons": {
            "shim_definition_reference": len(scan["shim_definition_references"]),
            "runtime_rewrite_candidates_not_applied_by_default": len(scan["rewrite_candidates"]),
        },
        "warnings": ["rewrite_candidates_present"] if scan["rewrite_candidates"] else [],
        "output_paths": {},
    }
    return payload


def run_import_rewrite_dry_run() -> dict[str, Any]:
    return build_import_rewrite_dry_run_payload()


def _render_current_status(payload: dict[str, Any]) -> str:
    lines = [
        "# Git Deletion Current Status",
        "",
        f"- status: `{payload.get('status')}`",
        f"- deleted_tracked_count: `{payload.get('deleted_tracked_count')}`",
        f"- untracked_count: `{payload.get('untracked_count')}`",
        f"- modified_count: `{payload.get('modified_count')}`",
        "",
        "## Deleted Modules",
        "",
    ]
    lines.extend([f"- {name}" for name in payload.get("deleted_module_names") or []] or ["- none"])
    lines.extend(["", "## Excluded Non-Deleted Migration Candidates", ""])
    lines.extend([f"- {name}" for name in payload.get("excluded_non_deleted_migration_candidates") or []] or ["- none"])
    return "\n".join(lines) + "\n"


def _render_alias(payload: dict[str, Any]) -> str:
    lines = [
        "# Import Alias Reconciliation",
        "",
        f"- status: `{payload.get('status')}`",
        f"- deleted_module_count: `{payload.get('deleted_module_count')}`",
        f"- alias_import_ok_count: `{payload.get('alias_import_ok_count')}`",
        f"- alias_import_fail_count: `{payload.get('alias_import_fail_count')}`",
        f"- from_import_ok_count: `{payload.get('from_import_ok_count')}`",
        f"- from_import_fail_count: `{payload.get('from_import_fail_count')}`",
        f"- retired_deleted_module_count: `{payload.get('retired_deleted_module_count')}`",
        f"- uncovered_deleted_module_count: `{payload.get('uncovered_deleted_module_count')}`",
        f"- recommendation: `{payload.get('recommendation')}`",
        f"- next_phase_scope: `{payload.get('next_phase_scope')}`",
        "",
        "## Failed Aliases",
        "",
    ]
    failed = payload.get("failed_aliases") or []
    lines.extend([f"- `{item.get('module_name')}` -> `{item.get('new_module_path')}` {item.get('error')}" for item in failed] or ["- none"])
    lines.extend(["", "## Retired Deleted Modules", ""])
    retired = payload.get("retired_deleted_modules") or []
    lines.extend([f"- `{item}`" for item in retired] or ["- none"])
    lines.extend(["", "## Uncovered Deleted Modules", ""])
    uncovered = payload.get("uncovered_deleted_modules") or []
    lines.extend([f"- `{item}`" for item in uncovered] or ["- none"])
    return "\n".join(lines) + "\n"


def _render_rewrite(payload: dict[str, Any]) -> str:
    lines = [
        "# Import Rewrite Dry Run",
        "",
        f"- status: `{payload.get('status')}`",
        f"- old_import_references_before: `{payload.get('old_import_references_before')}`",
        f"- rewrite_candidates: `{len(payload.get('rewrite_candidates') or [])}`",
        f"- rewritten_import_count: `{payload.get('rewritten_import_count')}`",
        f"- skipped_import_count: `{payload.get('skipped_import_count')}`",
        f"- remaining_legacy_import_count: `{payload.get('remaining_legacy_import_count')}`",
        "",
        "## Rewrite Candidates",
        "",
    ]
    candidates = payload.get("rewrite_candidates") or []
    lines.extend([f"- `{item.get('file')}:{item.get('line')}` {item.get('old_import')} -> {item.get('new_module_path')}" for item in candidates] or ["- none"])
    return "\n".join(lines) + "\n"


def _render_manifest(payload: dict[str, Any]) -> str:
    lines = [
        "# Import Rewrite Manifest",
        "",
        f"- entry_count: `{payload.get('entry_count')}`",
        f"- suspected_import_rewrite_root_cause: `{payload.get('suspected_import_rewrite_root_cause')}`",
        "",
        "## Core Strategy Files Rewritten",
        "",
    ]
    lines.extend([f"- {item}" for item in payload.get("core_strategy_files_rewritten") or []] or ["- none"])
    lines.extend(["", "## Entries", ""])
    for item in (payload.get("entries") or [])[:200]:
        lines.append(f"- `{item.get('file')}` `{item.get('old_import')}` -> `{item.get('new_import')}`")
    return "\n".join(lines) + "\n"


def write_json_and_report(payload: dict[str, Any], json_path: Path, md_path: Path, renderer: Any) -> dict[str, Any]:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_safe(dict(payload))
    payload["output_paths"] = {"json": _rel(json_path), "markdown": _rel(md_path)}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    md_path.write_text(renderer(payload), encoding="utf-8")
    return payload


def write_current_status_outputs(payload: dict[str, Any]) -> dict[str, Any]:
    return write_json_and_report(payload, CURRENT_STATUS_JSON, CURRENT_STATUS_MD, _render_current_status)


def write_alias_outputs(payload: dict[str, Any]) -> dict[str, Any]:
    return write_json_and_report(payload, ALIAS_JSON, ALIAS_MD, _render_alias)


def write_rewrite_outputs(payload: dict[str, Any]) -> dict[str, Any]:
    return write_json_and_report(payload, REWRITE_JSON, REWRITE_MD, _render_rewrite)


def write_rewrite_manifest_outputs(payload: dict[str, Any]) -> dict[str, Any]:
    return write_json_and_report(payload, REWRITE_MANIFEST_JSON, REWRITE_MANIFEST_MD, _render_manifest)


def run_import_alias_reconciliation(*, scan_only: bool = False) -> dict[str, Any]:
    current = write_current_status_outputs(get_current_git_deletion_status())
    scan = scan_old_import_references(current["deleted_module_names"])
    alias = check_alias_coverage(current["deleted_module_names"], reference_scan=scan)
    rewrite = write_rewrite_outputs(build_import_rewrite_dry_run_payload(current, scan))
    manifest = write_rewrite_manifest_outputs(build_import_rewrite_manifest(current["deleted_module_names"]))
    payload = dict(alias)
    payload["scan_only"] = bool(scan_only)
    payload["old_import_reference_count"] = scan["old_import_reference_count"]
    payload["old_import_references"] = scan["old_import_references"]
    payload["references_by_file"] = scan["references_by_file"]
    payload["references_by_module"] = scan["references_by_module"]
    payload["test_references"] = scan["test_references"]
    payload["script_references"] = scan["script_references"]
    payload["package_references"] = scan["package_references"]
    payload["shim_definition_references"] = scan["shim_definition_references"]
    payload["rewrite_candidates"] = scan["rewrite_candidates"]
    payload["do_not_rewrite"] = scan["do_not_rewrite"]
    payload["current_status_output_paths"] = current.get("output_paths", {})
    payload["rewrite_output_paths"] = rewrite.get("output_paths", {})
    payload["import_rewrite_manifest_output_paths"] = manifest.get("output_paths", {})
    payload["import_rewrite_manifest_entry_count"] = manifest.get("entry_count")
    payload["suspected_import_rewrite_root_cause"] = manifest.get("suspected_import_rewrite_root_cause")
    return write_alias_outputs(payload)
