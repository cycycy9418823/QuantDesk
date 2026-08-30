from __future__ import annotations

import json
import re
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.maintenance.import_alias_reconciliation import (
    check_alias_coverage,
    deleted_module_names_from_paths,
    scan_old_import_references,
)
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "git_deletion_audit_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "git_deletion_audit_latest.md"
DISCLAIMER = "This audit does not modify git state."

RECENT_ZIPS = [
    "quantdesk_clean_v3.7.0-data-backbone.zip",
    "quantdesk_clean_v3.7.0.1-strategy-viability-on-data-backbone.zip",
    "quantdesk_clean_v3.7.0.2-strategy-pipeline-repair.zip",
    "quantdesk_clean_v3.7.0.3-low-price-pipeline-finalization.zip",
    "quantdesk_clean_v3.7.0.4-capital-lot-sensitivity-and-git-hygiene.zip",
    "quantdesk_clean_v3.7.0.5-git-deletion-reconciliation.zip",
]

HIGH_RISK_PATTERNS = (
    "agent_",
    "paper_trading",
    "paper_review",
    "strategy_trade_replay",
    "backtest_statistical_validation",
    "feature_quality_audit",
    "intraday_",
    "conditional_trade_plan",
)


def _rel(path: str | Path) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _run_git_status() -> list[str]:
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


def _zip_entries(zip_path: Path) -> set[str]:
    if not zip_path.exists():
        return set()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return {name.replace("\\", "/") for name in zf.namelist() if not name.endswith("/")}
    except Exception:
        return set()


def _recent_release_entries() -> tuple[dict[str, list[str]], set[str]]:
    dist = PROJECT_ROOT / "dist"
    per_zip: dict[str, list[str]] = {}
    all_entries: set[str] = set()
    for name in RECENT_ZIPS:
        path = dist / name
        entries = _zip_entries(path)
        if entries:
            per_zip[name] = sorted(entries)
            all_entries.update(entries)
    return per_zip, all_entries


def _module_name(path: str) -> str:
    if not path.endswith(".py"):
        return ""
    return path[:-3].replace("/", ".").replace("\\", ".")


def _root_module_name(path: str) -> str:
    if path.startswith("quantdesk/") and path.count("/") == 1 and path.endswith(".py"):
        return Path(path).stem
    return ""


def _iter_source_files() -> list[Path]:
    roots = ["quantdesk", "scripts", "tests", "app", "docs"]
    files: list[Path] = []
    for root_name in roots:
        root = PROJECT_ROOT / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {".git", "__pycache__", "results", "reports", "dist", ".quantdesk_test_tmp"} for part in path.parts):
                continue
            if path.suffix.lower() in {".py", ".js", ".html", ".md", ".yaml", ".yml", ".json", ".ps1", ".cmd", ".sh"}:
                files.append(path)
    return files


def _iter_docs_files() -> list[Path]:
    candidates: list[Path] = []
    roots = [PROJECT_ROOT / "docs", PROJECT_ROOT]
    for root in roots:
        if not root.exists():
            continue
        iterator = root.rglob("*") if root.name == "docs" else root.glob("*")
        for path in iterator:
            if not path.is_file():
                continue
            if path.suffix.lower() in {".md", ".txt"} or path.name in {"CHANGELOG.md", "README.md", "ROADMAP.md", "architecture_inventory.md"}:
                candidates.append(path)
    return sorted(set(candidates))


def _build_docs_reference_index(module_names: list[str]) -> dict[str, list[str]]:
    names = sorted({name for name in module_names if name})
    refs_by_name: dict[str, list[str]] = {name: [] for name in names}
    if not names:
        return refs_by_name
    needles_by_name = {
        name: (name, f"quantdesk/{name}.py", f"quantdesk.{name}")
        for name in names
    }
    for path in _iter_docs_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = _rel(path)
        for name, needles in needles_by_name.items():
            if any(needle in text for needle in needles):
                refs_by_name[name].append(rel)
    return {name: sorted(set(refs)) for name, refs in refs_by_name.items()}


def _docs_only_reference_files(module_name: str, reference_index: dict[str, list[str]] | None = None) -> list[str]:
    refs: list[str] = []
    if not module_name:
        return refs
    if reference_index is not None:
        return list(reference_index.get(module_name) or [])
    needles = [module_name, f"quantdesk/{module_name}.py", f"quantdesk.{module_name}"]
    for path in _iter_docs_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if any(needle in text for needle in needles):
            refs.append(_rel(path))
    return sorted(set(refs))


def find_references_for_deleted(path: str, source_files: list[Path] | None = None) -> dict[str, Any]:
    return build_reference_index_for_deleted([path], source_files=source_files).get(
        path,
        {"module": "", "referenced_by": [], "reference_count": 0, "import_risk": "low"},
    )


def _reference_patterns_for_deleted(path: str) -> tuple[str, str, list[re.Pattern[str]]]:
    module = _module_name(path)
    stem = Path(path).stem
    if not module:
        return "", "", []
    parent = module.rsplit(".", 1)[0] if "." in module else ""
    norm_path = path.replace("\\", "/")
    return module, stem, [
        re.compile(rf"\bimport\s+{re.escape(module)}\b"),
        re.compile(rf"\bfrom\s+{re.escape(module)}\s+import\b"),
        re.compile(rf"\bfrom\s+{re.escape(parent)}\s+import\s+.*\b{re.escape(stem)}\b") if parent else re.compile(r"a^"),
        re.compile(rf"{re.escape(module)}"),
        re.compile(rf"{re.escape(norm_path)}"),
    ]


def _is_runtime_reference_path(path: str) -> bool:
    if path.startswith(("docs/", "tests/")):
        return False
    if path.startswith("scripts/legacy/"):
        return False
    if path.startswith("reports/") or path.startswith("results/"):
        return False
    return True


def _is_retired_legacy_delete_path(path: str) -> bool:
    name = Path(path).name
    return (
        path.startswith("quantdesk/v5_daily_report/")
        or path.startswith("scripts/legacy/run_v")
        or path.startswith("tests/test_v5")
        or path.startswith("tests/test_v6")
        or bool(re.fullmatch(r"scripts/run_v\d+.*\.py", path))
        or bool(re.fullmatch(r"scripts/(show|test|export|import|init)_v\d+.*\.py", path))
        or bool(re.fullmatch(r"tests/test_v\d+.*\.py", path))
        or name.startswith("run_v")
    )


def build_reference_index_for_deleted(paths: list[str], source_files: list[Path] | None = None) -> dict[str, dict[str, Any]]:
    specs: dict[str, tuple[str, str, list[re.Pattern[str]]]] = {
        path: _reference_patterns_for_deleted(path)
        for path in paths
    }
    referenced_by_path: dict[str, list[str]] = {path: [] for path in paths}
    runtime_referenced_by_path: dict[str, list[str]] = {path: [] for path in paths}
    for source in source_files or _iter_source_files():
        rel = _rel(source)
        try:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for path, (module, stem, patterns) in specs.items():
            if rel == path or not patterns:
                continue
            norm_path = path.replace("\\", "/")
            if module not in text and norm_path not in text:
                continue
            if any(pattern.search(text) for pattern in patterns):
                referenced_by_path[path].append(rel)
                if _is_runtime_reference_path(rel):
                    runtime_referenced_by_path[path].append(rel)
    out: dict[str, dict[str, Any]] = {}
    for path, (module, _stem, _patterns) in specs.items():
        referenced = sorted(set(referenced_by_path.get(path) or []))
        runtime_referenced = sorted(set(runtime_referenced_by_path.get(path) or []))
        out[path] = {
            "module": module,
            "referenced_by": referenced[:50],
            "reference_count": len(referenced),
            "runtime_referenced_by": runtime_referenced[:50],
            "runtime_reference_count": len(runtime_referenced),
            "import_risk": "high" if runtime_referenced else "low",
        }
    return out



def _classify_deleted(path: str, current_entries: set[str], previous_entries: set[str], references: dict[str, Any]) -> dict[str, Any]:
    name = Path(path).name
    high_hint = any(token in name for token in HIGH_RISK_PATTERNS)
    existed_current = path in current_entries
    existed_previous = path in previous_entries
    if references.get("import_risk") == "high":
        bucket = "unknown_risk"
        recommendation = "restore_before_continue"
    elif _is_retired_legacy_delete_path(path):
        bucket = "likely_legacy_removed"
        recommendation = "ignore_as_legacy_removed"
    elif existed_current or existed_previous or high_hint:
        bucket = "unknown_risk"
        recommendation = "needs_manual_review"
    else:
        bucket = "likely_legacy_removed"
        recommendation = "ignore_as_legacy_removed"
    return {
        "path": path,
        "existed_in_current_clean_zip": bool(existed_current),
        "existed_in_previous_release_zip_if_available": bool(existed_previous),
        "missing_from_recent_release_series": not bool(existed_current or existed_previous),
        "classification": bucket,
        "recommendation": recommendation,
        "risk_hint": "high_risk_name_pattern" if high_hint else "",
        **references,
    }


def run_git_deletion_audit() -> dict[str, Any]:
    lines = _run_git_status()
    parsed = parse_git_status(lines)
    per_zip, previous_entries = _recent_release_entries()
    current_entries = _zip_entries(PROJECT_ROOT / "dist" / "quantdesk_clean_v3.7.0.5-git-deletion-reconciliation.zip")
    if not current_entries:
        current_entries = _zip_entries(PROJECT_ROOT / "dist" / "quantdesk_clean_v3.7.0.4-capital-lot-sensitivity-and-git-hygiene.zip")
    comparison_status = "available" if per_zip or current_entries else "not_available"
    source_files = _iter_source_files()
    deleted_module_names = deleted_module_names_from_paths(parsed["deleted_tracked_files"])
    docs_reference_index = _build_docs_reference_index([_root_module_name(path) for path in parsed["deleted_tracked_files"]])
    alias_payload = check_alias_coverage(deleted_module_names)
    alias_by_name = {
        item.get("module_name"): item
        for item in (
            (alias_payload.get("shim_covered_aliases") or [])
            + (alias_payload.get("failed_aliases") or [])
            + (alias_payload.get("uncovered_deleted_module_details") or [])
            + (alias_payload.get("retired_deleted_module_details") or [])
        )
    }
    scan_payload = scan_old_import_references(deleted_module_names)
    deleted_details = []
    shim_covered_deleted_files: list[str] = []
    broken_alias_deleted_files: list[str] = []
    retired_legacy_deleted_files: list[str] = []
    deprecated_legacy_paths: list[str] = []
    restore_required_files: list[str] = []
    docs_only_references: list[dict[str, Any]] = []
    shim_definition_references: list[dict[str, Any]] = scan_payload.get("shim_definition_references") or []
    reference_index = build_reference_index_for_deleted(parsed["deleted_tracked_files"], source_files=source_files)
    for path in parsed["deleted_tracked_files"]:
        refs = reference_index.get(path, {"module": "", "referenced_by": [], "reference_count": 0, "import_risk": "low"})
        item = _classify_deleted(path, current_entries, previous_entries, refs)
        root_name = _root_module_name(path)
        alias_item = alias_by_name.get(root_name, {})
        docs_refs = _docs_only_reference_files(root_name, docs_reference_index)
        if docs_refs:
            docs_only_references.append({"path": path, "module_name": root_name, "referenced_by": docs_refs})
        if alias_item and alias_item.get("status") == "covered":
            item.update(
                {
                    "classification": "resolved_by_compat_shim",
                    "risk": "low",
                    "restore_required": False,
                    "fix_alias_required": False,
                    "recommendation": "no_restore_needed_deprecated_alias_supported",
                    "alias": alias_item,
                }
            )
            shim_covered_deleted_files.append(path)
            deprecated_legacy_paths.append(path)
        elif alias_item:
            if alias_item.get("status") == "retired_deleted":
                item.update(
                    {
                        "classification": "retired_legacy_deleted",
                        "risk": "low",
                        "restore_required": False,
                        "fix_alias_required": False,
                        "recommendation": "no_restore_needed_retired_legacy_unreferenced",
                        "alias": alias_item,
                    }
                )
                retired_legacy_deleted_files.append(path)
                deprecated_legacy_paths.append(path)
            else:
                item.update(
                    {
                        "classification": "broken_alias",
                        "risk": "high",
                        "restore_required": False,
                        "fix_alias_required": True,
                        "recommendation": "fix_alias_not_restore_all",
                        "alias": alias_item,
                    }
                )
                broken_alias_deleted_files.append(path)
        deleted_details.append(item)
    runtime_import_references = scan_payload.get("rewrite_candidates") or []
    high_risk = [item for item in deleted_details if item.get("classification") == "broken_alias"]
    likely_legacy = [
        item
        for item in deleted_details
        if item.get("classification") in {"likely_legacy_removed", "resolved_by_compat_shim", "retired_legacy_deleted"}
    ]
    warnings: list[str] = []
    if parsed["deleted_tracked_files"]:
        warnings.append("deleted_tracked_files_present")
    if comparison_status == "not_available":
        warnings.append("release_zip_comparison_not_available")
    if broken_alias_deleted_files:
        warnings.append("broken_alias_deleted_files_need_fix")
    if runtime_import_references:
        warnings.append("runtime_legacy_import_references_present")
    recommendation = "safe_to_continue_after_alias_reconciliation"
    if broken_alias_deleted_files:
        recommendation = "fix_broken_aliases_only"
    elif not shim_covered_deleted_files and parsed["deleted_tracked_files"] and not retired_legacy_deleted_files:
        recommendation = "manual_review_required"
    status = "warning" if warnings else "passed"
    payload = {
        "ok": True,
        "status": status,
        "task_type": "git_deletion_audit",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "deleted_tracked_count": len(parsed["deleted_tracked_files"]),
        "untracked_count": len(parsed["untracked_files"]),
        "modified_count": len(parsed["modified_files"]),
        "renamed_count": len(parsed["renamed_files"]),
        "added_count": len(parsed["added_files"]),
        "unknown_risk_count": len([item for item in deleted_details if item.get("classification") not in {"resolved_by_compat_shim", "likely_legacy_removed", "retired_legacy_deleted", "broken_alias"}]),
        "deleted_tracked_files": parsed["deleted_tracked_files"],
        "untracked_files": parsed["untracked_files"],
        "modified_files": parsed["modified_files"],
        "renamed_files": parsed["renamed_files"],
        "added_files": parsed["added_files"],
        "deleted_file_details": deleted_details,
        "high_risk_deleted_files": [item["path"] for item in high_risk],
        "shim_covered_deleted_files": shim_covered_deleted_files,
        "broken_alias_deleted_files": broken_alias_deleted_files,
        "retired_legacy_deleted_files": retired_legacy_deleted_files,
        "docs_only_references": docs_only_references,
        "runtime_import_references": runtime_import_references,
        "shim_definition_references": shim_definition_references,
        "restore_required_files": restore_required_files,
        "deprecated_legacy_paths": deprecated_legacy_paths,
        "likely_legacy_removed_files": [item["path"] for item in likely_legacy],
        "import_reference_risks": [
            {"path": item["path"], "module": item.get("module"), "reference_count": item.get("reference_count"), "referenced_by": item.get("referenced_by")}
            for item in deleted_details
            if item.get("import_risk") == "high"
        ],
        "comparison_status": comparison_status,
        "release_zips_compared": sorted(per_zip.keys()),
        "alias_reconciliation": {
            "deleted_module_count": alias_payload.get("deleted_module_count"),
            "alias_import_ok_count": alias_payload.get("alias_import_ok_count"),
            "alias_import_fail_count": alias_payload.get("alias_import_fail_count"),
            "from_import_ok_count": alias_payload.get("from_import_ok_count"),
            "from_import_fail_count": alias_payload.get("from_import_fail_count"),
            "retired_deleted_module_count": alias_payload.get("retired_deleted_module_count"),
            "retired_deleted_modules": alias_payload.get("retired_deleted_modules"),
            "uncovered_deleted_module_count": alias_payload.get("uncovered_deleted_module_count"),
            "uncovered_deleted_modules": alias_payload.get("uncovered_deleted_modules"),
            "failed_aliases": alias_payload.get("failed_aliases"),
        },
        "manual_review_required": bool(broken_alias_deleted_files),
        "recommendation": recommendation,
        "warnings": warnings,
        "warning_count": len(warnings),
        "warnings_top": warnings[:10],
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return payload


def render_git_deletion_audit_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Git Deletion Audit",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- deleted_tracked_count: `{payload.get('deleted_tracked_count')}`",
        f"- untracked_count: `{payload.get('untracked_count')}`",
        f"- modified_count: `{payload.get('modified_count')}`",
        f"- unknown_risk_count: `{payload.get('unknown_risk_count')}`",
        f"- recommendation: `{payload.get('recommendation')}`",
        f"- comparison_status: `{payload.get('comparison_status')}`",
        "",
        "## Shim-Covered Deleted Files",
        "",
    ]
    shim_covered = payload.get("shim_covered_deleted_files") or []
    lines.extend([f"- {item}" for item in shim_covered] if shim_covered else ["- none"])
    lines.extend([
        "",
        "## Retired Legacy Deleted Files",
        "",
    ])
    retired = payload.get("retired_legacy_deleted_files") or []
    lines.extend([f"- {item}" for item in retired] if retired else ["- none"])
    lines.extend([
        "",
        "## Broken Alias Deleted Files",
        "",
    ])
    broken = payload.get("broken_alias_deleted_files") or []
    lines.extend([f"- {item}" for item in broken] if broken else ["- none"])
    lines.extend([
        "",
        "## High Risk Deleted Files",
        "",
    ])
    high = payload.get("high_risk_deleted_files") or []
    lines.extend([f"- {item}" for item in high] if high else ["- none"])
    lines.extend(["", "## Runtime Import References", ""])
    runtime_refs = payload.get("runtime_import_references") or []
    if runtime_refs:
        for item in runtime_refs:
            lines.append(f"- `{item.get('file')}:{item.get('line')}` {item.get('old_import')} -> {item.get('new_module_path')}")
    else:
        lines.append("- none")
    lines.extend(["", "## Docs-Only References", ""])
    docs_refs = payload.get("docs_only_references") or []
    if docs_refs:
        for item in docs_refs:
            lines.append(f"- `{item.get('path')}` referenced_by={item.get('referenced_by')}")
    else:
        lines.append("- none")
    lines.extend(["", "## Import Reference Risks", ""])
    risks = payload.get("import_reference_risks") or []
    if risks:
        for item in risks:
            lines.append(f"- `{item.get('path')}` reference_count={item.get('reference_count')} referenced_by={item.get('referenced_by')}")
    else:
        lines.append("- none")
    lines.extend(["", "## Likely Legacy Removed", ""])
    legacy = payload.get("likely_legacy_removed_files") or []
    lines.extend([f"- {item}" for item in legacy[:80]] if legacy else ["- none"])
    lines.extend(["", "## Warnings", ""])
    warnings = payload.get("warnings") or []
    lines.extend([f"- {item}" for item in warnings] if warnings else ["- none"])
    return "\n".join(lines) + "\n"


def write_git_deletion_audit_outputs(payload: dict[str, Any], *, result_path: str | Path = RESULT_JSON, report_path: str | Path = REPORT_MD) -> dict[str, Any]:
    result = Path(result_path)
    report = Path(report_path)
    result.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report.write_text(render_git_deletion_audit_markdown(payload), encoding="utf-8")
    return payload
