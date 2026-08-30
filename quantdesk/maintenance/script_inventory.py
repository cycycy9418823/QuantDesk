from __future__ import annotations

import ast
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import write_json, write_md
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "maintenance" / "script_inventory_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "maintenance" / "script_inventory_latest.md"
DISCLAIMER = "Read-only inventory. This diagnostic does not delete, move, rewrite, or archive scripts."

SCRIPT_SUFFIXES = {".py", ".ps1", ".cmd", ".bat", ".sh"}
VERSION_TOKEN_RE = re.compile(r"(?:^|_)(v\d{1,4})(?:_|$)", re.IGNORECASE)
VERSIONED_PREFIX_RE = re.compile(r"^(run|show|build|check|validate|export|import|test)_v\d{1,4}(?:_|$)", re.IGNORECASE)
PREFIXES = (
    "run_",
    "show_",
    "build_",
    "check_",
    "validate_",
    "update_",
    "export_",
    "import_",
    "generate_",
    "render_",
)


@dataclass(frozen=True)
class ScriptRecord:
    path: str
    name: str
    suffix: str
    category: str
    entrypoint_status: str
    qd_commands: list[str]
    maintenance_subcommands: list[str]
    version_label: str
    family: str
    line_count: int
    reference_count: int
    reference_groups: dict[str, int]
    referenced_by: list[str]
    recommendation: str
    risk_level: str
    archive_review_candidate: bool


def _rel(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(root)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _line_count(path: Path) -> int:
    text = _read_text(path)
    if not text:
        return 0
    return len(text.splitlines())


def _extract_literal_assignment(path: Path, name: str) -> Any:
    text = _read_text(path)
    if not text:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in tree.body:
        value_node: ast.AST | None = None
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            value_node = node.value
        if value_node is not None:
            try:
                return ast.literal_eval(value_node)
            except Exception:
                return None
    return None


def load_qd_commands(project_root: Path = PROJECT_ROOT) -> dict[str, dict[str, Any]]:
    raw = _extract_literal_assignment(project_root / "scripts" / "qd.py", "COMMANDS")
    if not isinstance(raw, dict):
        return {}
    commands: dict[str, dict[str, Any]] = {}
    for command, spec in raw.items():
        if not isinstance(command, str) or not isinstance(spec, tuple) or len(spec) < 3:
            continue
        script, defaults, description = spec[:3]
        if not isinstance(script, str):
            continue
        commands[command] = {
            "script": script,
            "defaults": list(defaults) if isinstance(defaults, list) else [],
            "description": str(description),
        }
    return commands


def _call_string_arg(call: ast.Call) -> str:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return ""


def load_maintenance_subcommands(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    path = project_root / "scripts" / "run_maintenance.py"
    text = _read_text(path)
    if not text:
        return {"subcommands": [], "script_names": []}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {"subcommands": [], "script_names": []}
    subcommands: set[str] = set()
    script_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("run_"):
            script_names.add(f"{node.module}.py")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_parser":
            value = _call_string_arg(node)
            if value:
                subcommands.add(value)
    return {"subcommands": sorted(subcommands), "script_names": sorted(script_names)}


def _version_label(stem: str) -> str:
    match = VERSION_TOKEN_RE.search(stem)
    return match.group(1).lower() if match else ""


def _category(stem: str, suffix: str) -> str:
    lower = stem.lower()
    if suffix != ".py":
        return f"shell_{suffix.lstrip('.')}"
    if lower == "qd":
        return "unified_cli"
    if VERSIONED_PREFIX_RE.search(lower):
        prefix = lower.split("_", 1)[0]
        return f"versioned_{prefix}"
    if VERSION_TOKEN_RE.search(lower):
        return "versioned_other"
    for prefix in PREFIXES:
        if lower.startswith(prefix):
            return f"{prefix.rstrip('_')}_other"
    if lower.endswith("_server"):
        return "server"
    if lower.endswith("_ui") or lower in {"launcher", "live_monitor"}:
        return "ui_or_launcher"
    if lower.startswith("screen_") or lower.startswith("scan_"):
        return "scan_other"
    return "other"


def _family(stem: str) -> str:
    lower = stem.lower()
    for prefix in PREFIXES:
        if lower.startswith(prefix):
            lower = lower[len(prefix):]
            break
    lower = re.sub(r"^v\d{1,4}_?", "", lower)
    lower = re.sub(r"_{2,}", "_", lower).strip("_")
    return lower or stem.lower()


def _entrypoint_status(
    name: str,
    stem: str,
    qd_script_to_commands: dict[str, list[str]],
    maintenance_scripts: set[str],
) -> str:
    if name == "qd.py":
        return "unified_cli"
    if name in qd_script_to_commands:
        return "qd_command_script"
    if name in maintenance_scripts:
        return "maintenance_subcommand_script"
    if _version_label(stem):
        return "historical_versioned_raw_script"
    return "raw_script_only"


def _recommendation(status: str, category: str) -> tuple[str, str]:
    if status == "unified_cli":
        return "keep_as_primary_operator_entrypoint", "low"
    if status == "qd_command_script":
        return "keep_behind_qd_py_command", "low"
    if status == "maintenance_subcommand_script":
        return "keep_as_read_only_maintenance_subcommand", "low"
    if status == "historical_versioned_raw_script":
        return "review_for_qd_promotion_or_legacy_archive", "medium"
    if category.startswith("run_") or category.startswith("versioned_"):
        return "review_for_command_registry_or_deprecation_note", "medium"
    return "keep_if_referenced_else_review_later", "low"


def _iter_script_files(project_root: Path) -> list[Path]:
    scripts_dir = project_root / "scripts"
    if not scripts_dir.exists():
        return []
    return sorted(path for path in scripts_dir.iterdir() if path.is_file() and path.suffix.lower() in SCRIPT_SUFFIXES)


def _iter_reference_files(project_root: Path) -> list[Path]:
    suffixes = {
        ".py",
        ".md",
        ".txt",
        ".rst",
        ".ps1",
        ".cmd",
        ".bat",
        ".sh",
        ".js",
        ".html",
        ".yaml",
        ".yml",
        ".toml",
        ".nsi",
        ".spec",
    }
    roots = [
        project_root / "README.md",
        project_root / "AGENTS.md",
        project_root / "pyproject.toml",
        project_root / "requirements.txt",
        project_root / "requirements-dev.txt",
        project_root / "requirements-data.txt",
        project_root / "export_project.ps1",
        project_root / "export_project.cmd",
        project_root / "run_tests.cmd",
        project_root / "self_check.cmd",
        project_root / ".github",
        project_root / "app",
        project_root / "build",
        project_root / "config",
        project_root / "docs",
        project_root / "quantdesk",
        project_root / "scripts",
        project_root / "tests",
        project_root / "tools",
    ]
    excluded_parts = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "data",
        "dist",
        "logs",
        "output",
        "reports",
        "results",
        "state",
        "temp",
        "test_runtime",
        "_artifacts",
        "QuantDesk",
    }
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in candidates:
            if any(part in excluded_parts for part in path.parts):
                continue
            if path.suffix.lower() in suffixes:
                files.append(path)
    return sorted(set(files))


def _reference_group(rel: str) -> str:
    if rel.startswith("tests/"):
        return "tests"
    if rel.startswith("docs/") or rel in {"README.md", "AGENTS.md"}:
        return "docs"
    if rel.startswith("scripts/"):
        return "scripts"
    if rel.startswith("quantdesk/"):
        return "package"
    if rel.startswith("app/"):
        return "frontend"
    if rel.startswith("config/"):
        return "config"
    if rel.startswith("build/") or rel.startswith(".github/") or rel.startswith("tools/"):
        return "build_or_ci"
    return "root"


def _reference_tokens(record: ScriptRecord) -> list[str]:
    stem = record.name.rsplit(".", 1)[0]
    tokens = {record.path, record.path.replace("/", "\\"), record.name}
    if len(stem) >= 8 and "_" in stem:
        tokens.add(stem)
    return sorted(tokens, key=len, reverse=True)


def build_reference_index(records: list[ScriptRecord], project_root: Path = PROJECT_ROOT) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {
        record.path: {"reference_count": 0, "reference_groups": {}, "referenced_by": []}
        for record in records
    }
    token_map = {record.path: _reference_tokens(record) for record in records}
    for source in _iter_reference_files(project_root):
        rel = _rel(source, root=project_root)
        try:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text:
            continue
        for record in records:
            if rel == record.path:
                continue
            if not any(token in text for token in token_map[record.path]):
                continue
            item = index[record.path]
            group = _reference_group(rel)
            if rel not in item["referenced_by"]:
                item["referenced_by"].append(rel)
                item["reference_groups"][group] = int(item["reference_groups"].get(group, 0)) + 1
    for item in index.values():
        item["referenced_by"] = sorted(item["referenced_by"])[:30]
        item["reference_groups"] = dict(sorted(item["reference_groups"].items()))
        item["reference_count"] = sum(item["reference_groups"].values())
    return index


def _dependency_inventory(project_root: Path) -> dict[str, Any]:
    files = {
        "pyproject": project_root / "pyproject.toml",
        "runtime_requirements": project_root / "requirements.txt",
        "dev_requirements": project_root / "requirements-dev.txt",
        "data_requirements": project_root / "requirements-data.txt",
    }
    return {
        key: {"path": _rel(path, root=project_root), "exists": path.exists(), "line_count": _line_count(path)}
        for key, path in files.items()
    }


def _documentation_inventory(project_root: Path) -> dict[str, Any]:
    files = {
        "readme": project_root / "README.md",
        "quickstart": project_root / "docs" / "quickstart.md",
        "script_entrypoint_inventory": project_root / "docs" / "script_entrypoint_inventory.md",
        "architecture": project_root / "docs" / "architecture.md",
        "troubleshooting": project_root / "docs" / "troubleshooting_quick_ref.md",
    }
    return {
        key: {"path": _rel(path, root=project_root), "exists": path.exists(), "line_count": _line_count(path)}
        for key, path in files.items()
    }


def _risk_sections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    dependency_missing = [
        item["path"]
        for item in payload["dependency_inventory"].values()
        if not item.get("exists")
    ]
    doc_missing = [
        item["path"]
        for item in payload["documentation_inventory"].values()
        if not item.get("exists")
    ]
    return [
        {
            "risk": "script_redundancy_and_version_conflict",
            "status": "warning" if payload["versioned_raw_script_count"] else "passed",
            "evidence": {
                "script_count": payload["script_count"],
                "versioned_script_count": payload["versioned_script_count"],
                "versioned_raw_script_count": payload["versioned_raw_script_count"],
                "raw_script_count": payload["raw_script_count"],
            },
            "next_action": "Promote still-useful raw scripts to qd.py or archive them after reference and test audit.",
        },
        {
            "risk": "documentation_runbook_gap",
            "status": "warning" if doc_missing else "passed",
            "evidence": {"missing": doc_missing},
            "next_action": "Keep README, quickstart, and script inventory aligned with qd.py.",
        },
        {
            "risk": "dependency_reproducibility",
            "status": "warning" if dependency_missing else "passed",
            "evidence": {"missing": dependency_missing},
            "next_action": "Use pyproject.toml plus requirements files as the reproducible dependency contract.",
        },
        {
            "risk": "test_maintenance_cost",
            "status": "warning",
            "evidence": {
                "test_file_count": payload["test_file_count"],
                "suggested_tiers": ["targeted unittest", "self_check", "full unittest discover before release"],
            },
            "next_action": "Keep adding focused tests near changed maintenance areas; reserve full discover for release gates.",
        },
        {
            "risk": "frontend_backend_coupling",
            "status": "watch",
            "evidence": {
                "active_template": "app/templates/daily_research.html",
                "active_bundle": "app/static/js/daily_research.js",
                "active_server": "scripts/quant_api_server.py",
            },
            "next_action": "Keep the active daily-research JSON payload contract aligned before touching the retired 8765 bundle.",
        },
        {
            "risk": "data_quality_and_backtest_controls",
            "status": "watch",
            "evidence": {"protected_data_policy": "read-only inventory does not touch data/daily or canonical equity outputs"},
            "next_action": "Keep data-health, strategy-regression-audit, and reproducibility manifests in the gate.",
        },
    ]


def build_script_inventory(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    project_root = Path(project_root)
    qd_commands = load_qd_commands(project_root)
    qd_script_to_commands: dict[str, list[str]] = defaultdict(list)
    for command, spec in qd_commands.items():
        qd_script_to_commands[str(spec.get("script", ""))].append(command)
    maintenance = load_maintenance_subcommands(project_root)
    maintenance_scripts = set(maintenance.get("script_names") or [])
    base_records: list[ScriptRecord] = []
    for path in _iter_script_files(project_root):
        stem = path.stem
        suffix = path.suffix.lower()
        category = _category(stem, suffix)
        status = _entrypoint_status(path.name, stem, qd_script_to_commands, maintenance_scripts)
        recommendation, risk_level = _recommendation(status, category)
        base_records.append(
            ScriptRecord(
                path=_rel(path, root=project_root),
                name=path.name,
                suffix=suffix,
                category=category,
                entrypoint_status=status,
                qd_commands=sorted(qd_script_to_commands.get(path.name, [])),
                maintenance_subcommands=maintenance.get("subcommands", []) if path.name == "run_maintenance.py" else [],
                version_label=_version_label(stem),
                family=_family(stem),
                line_count=_line_count(path),
                reference_count=0,
                reference_groups={},
                referenced_by=[],
                recommendation=recommendation,
                risk_level=risk_level,
                archive_review_candidate=False,
            )
        )
    reference_index = build_reference_index(base_records, project_root)
    records: list[ScriptRecord] = []
    for item in base_records:
        refs = reference_index.get(item.path, {})
        reference_count = int(refs.get("reference_count") or 0)
        is_raw = item.entrypoint_status in {"historical_versioned_raw_script", "raw_script_only"}
        archive_review_candidate = bool(is_raw and reference_count == 0)
        recommendation = item.recommendation
        risk_level = item.risk_level
        if archive_review_candidate:
            recommendation = "archive_review_candidate_after_tests_and_release_audit"
            risk_level = "low"
        elif is_raw and reference_count > 0:
            recommendation = "referenced_raw_script_review_before_archive"
            risk_level = "medium"
        records.append(
            ScriptRecord(
                path=item.path,
                name=item.name,
                suffix=item.suffix,
                category=item.category,
                entrypoint_status=item.entrypoint_status,
                qd_commands=item.qd_commands,
                maintenance_subcommands=item.maintenance_subcommands,
                version_label=item.version_label,
                family=item.family,
                line_count=item.line_count,
                reference_count=reference_count,
                reference_groups=dict(refs.get("reference_groups") or {}),
                referenced_by=list(refs.get("referenced_by") or []),
                recommendation=recommendation,
                risk_level=risk_level,
                archive_review_candidate=archive_review_candidate,
            )
        )
    record_dicts = [asdict(item) for item in sorted(records, key=lambda item: item.path)]
    by_category = Counter(item.category for item in records)
    by_status = Counter(item.entrypoint_status for item in records)
    by_version = Counter(item.version_label for item in records if item.version_label)
    by_family: dict[str, list[str]] = defaultdict(list)
    for item in records:
        if item.version_label:
            by_family[item.family].append(item.path)
    duplicate_families = [
        {"family": family, "count": len(paths), "paths": sorted(paths)[:20]}
        for family, paths in sorted(by_family.items())
        if len(paths) > 1
    ]
    qd_target_scripts = {str(spec.get("script", "")) for spec in qd_commands.values()}
    script_names = {item.name for item in records}
    missing_qd_target_scripts = sorted(name for name in qd_target_scripts if name and name not in script_names)
    versioned_raw = [
        item
        for item in records
        if item.entrypoint_status == "historical_versioned_raw_script"
    ]
    raw_scripts = [
        item
        for item in records
        if item.entrypoint_status in {"historical_versioned_raw_script", "raw_script_only"}
    ]
    archive_review_candidates = [item for item in raw_scripts if item.archive_review_candidate]
    referenced_raw_scripts = [item for item in raw_scripts if item.reference_count > 0]
    reference_groups = Counter()
    for item in records:
        for group, count in item.reference_groups.items():
            reference_groups[group] += int(count)
    warnings: list[str] = []
    if missing_qd_target_scripts:
        warnings.append("missing_qd_target_scripts")
    if versioned_raw:
        warnings.append("versioned_raw_scripts_present")
    if len(raw_scripts) > 50:
        warnings.append("large_raw_script_surface")
    if duplicate_families:
        warnings.append("duplicate_versioned_families_present")
    if archive_review_candidates:
        warnings.append("archive_review_candidates_present")
    payload: dict[str, Any] = {
        "ok": True,
        "status": "warning" if warnings else "passed",
        "task_type": "script_inventory",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "script_count": len(records),
        "qd_command_count": len(qd_commands),
        "qd_command_script_count": len([item for item in records if item.entrypoint_status == "qd_command_script"]),
        "raw_script_count": len(raw_scripts),
        "versioned_script_count": len([item for item in records if item.version_label]),
        "versioned_raw_script_count": len(versioned_raw),
        "archive_review_candidate_count": len(archive_review_candidates),
        "referenced_raw_script_count": len(referenced_raw_scripts),
        "maintenance_subcommand_count": len(maintenance.get("subcommands") or []),
        "test_file_count": len(list((project_root / "tests").glob("test_*.py"))) if (project_root / "tests").exists() else 0,
        "counts_by_category": dict(sorted(by_category.items())),
        "counts_by_entrypoint_status": dict(sorted(by_status.items())),
        "counts_by_version": dict(sorted(by_version.items())),
        "counts_by_reference_group": dict(sorted(reference_groups.items())),
        "missing_qd_target_scripts": missing_qd_target_scripts,
        "maintenance_subcommands": maintenance.get("subcommands") or [],
        "duplicate_versioned_families": sorted(duplicate_families, key=lambda item: (-item["count"], item["family"]))[:50],
        "top_versioned_raw_scripts": [asdict(item) for item in sorted(versioned_raw, key=lambda item: item.path)[:80]],
        "archive_review_candidates": [
            asdict(item)
            for item in sorted(archive_review_candidates, key=lambda item: (-item.line_count, item.path))[:80]
        ],
        "referenced_raw_scripts": [
            asdict(item)
            for item in sorted(referenced_raw_scripts, key=lambda item: (-item.reference_count, item.path))[:80]
        ],
        "reference_summary": {
            "source_file_count": len(_iter_reference_files(project_root)),
            "referenced_script_count": len([item for item in records if item.reference_count > 0]),
            "unreferenced_script_count": len([item for item in records if item.reference_count == 0]),
            "referenced_raw_script_count": len(referenced_raw_scripts),
            "archive_review_candidate_count": len(archive_review_candidates),
            "note": "Zero-reference means no textual reference was found in source roots; it is not automatic deletion approval.",
        },
        "records": record_dicts,
        "dependency_inventory": _dependency_inventory(project_root),
        "documentation_inventory": _documentation_inventory(project_root),
        "recommendations": [
            "Use python scripts/qd.py help for normal operation; raw scripts are advanced or compatibility entrypoints.",
            "Do not delete versioned scripts until references, tests, and release artifact expectations are audited.",
            "Promote actively used versioned scripts to qd.py subcommands or document them as legacy compatibility.",
            "Use archive_review_candidates as a review queue, not as an automatic deletion list.",
            "Keep network, LLM, and full-A scans opt-in while cleaning entrypoints.",
        ],
        "warnings": warnings,
        "warning_count": len(warnings),
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    payload["risk_sections"] = _risk_sections(payload)
    return payload


def render_script_inventory_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Script Inventory",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- script_count: `{payload.get('script_count')}`",
        f"- qd_command_count: `{payload.get('qd_command_count')}`",
        f"- qd_command_script_count: `{payload.get('qd_command_script_count')}`",
        f"- raw_script_count: `{payload.get('raw_script_count')}`",
        f"- versioned_raw_script_count: `{payload.get('versioned_raw_script_count')}`",
        f"- archive_review_candidate_count: `{payload.get('archive_review_candidate_count')}`",
        f"- referenced_raw_script_count: `{payload.get('referenced_raw_script_count')}`",
        "",
        "## Counts By Category",
        "",
        "| category | count |",
        "| --- | ---: |",
    ]
    for category, count in (payload.get("counts_by_category") or {}).items():
        lines.append(f"| `{category}` | {count} |")
    lines.extend(["", "## Entrypoint Status", "", "| status | count |", "| --- | ---: |"])
    for status, count in (payload.get("counts_by_entrypoint_status") or {}).items():
        lines.append(f"| `{status}` | {count} |")
    lines.extend(["", "## Reference Summary", ""])
    summary = payload.get("reference_summary") or {}
    for key in [
        "source_file_count",
        "referenced_script_count",
        "unreferenced_script_count",
        "referenced_raw_script_count",
        "archive_review_candidate_count",
    ]:
        lines.append(f"- {key}: `{summary.get(key)}`")
    lines.append(f"- note: {summary.get('note')}")
    lines.extend(["", "### Counts By Reference Group", "", "| group | count |", "| --- | ---: |"])
    for group, count in (payload.get("counts_by_reference_group") or {}).items():
        lines.append(f"| `{group}` | {count} |")
    lines.extend(["", "## Risk Sections", ""])
    for item in payload.get("risk_sections") or []:
        lines.append(f"- `{item.get('risk')}` status=`{item.get('status')}` next=`{item.get('next_action')}`")
    lines.extend(["", "## Top Versioned Raw Scripts", ""])
    raw = payload.get("top_versioned_raw_scripts") or []
    if raw:
        for item in raw[:50]:
            lines.append(
                f"- `{item.get('path')}` version=`{item.get('version_label')}` family=`{item.get('family')}` "
                f"recommendation=`{item.get('recommendation')}`"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Duplicate Versioned Families", ""])
    families = payload.get("duplicate_versioned_families") or []
    if families:
        for item in families[:30]:
            lines.append(f"- `{item.get('family')}` count={item.get('count')} sample={item.get('paths')}")
    else:
        lines.append("- none")
    lines.extend(["", "## Archive Review Candidates", ""])
    candidates = payload.get("archive_review_candidates") or []
    if candidates:
        for item in candidates[:40]:
            lines.append(f"- `{item.get('path')}` lines={item.get('line_count')} recommendation=`{item.get('recommendation')}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Referenced Raw Scripts", ""])
    referenced = payload.get("referenced_raw_scripts") or []
    if referenced:
        for item in referenced[:40]:
            lines.append(
                f"- `{item.get('path')}` references={item.get('reference_count')} groups={item.get('reference_groups')} "
                f"sample={item.get('referenced_by')}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Missing qd.py Target Scripts", ""])
    missing = payload.get("missing_qd_target_scripts") or []
    lines.extend([f"- `{item}`" for item in missing] if missing else ["- none"])
    lines.extend(["", "## Recommendations", ""])
    lines.extend([f"- {item}" for item in payload.get("recommendations") or []] or ["- none"])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in payload.get("warnings") or []] if payload.get("warnings") else ["- none"])
    return "\n".join(lines) + "\n"


def write_script_inventory_outputs(
    payload: dict[str, Any] | None = None,
    *,
    result_path: str | Path = RESULT_JSON,
    report_path: str | Path = REPORT_MD,
) -> dict[str, Any]:
    payload = dict(payload or build_script_inventory())
    result = Path(result_path)
    report = Path(report_path)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    write_json(result, payload)
    write_md(report, render_script_inventory_markdown(payload))
    return payload


def compact_script_inventory_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "task_type": payload.get("task_type"),
        "script_count": payload.get("script_count"),
        "qd_command_count": payload.get("qd_command_count"),
        "qd_command_script_count": payload.get("qd_command_script_count"),
        "raw_script_count": payload.get("raw_script_count"),
        "versioned_script_count": payload.get("versioned_script_count"),
        "versioned_raw_script_count": payload.get("versioned_raw_script_count"),
        "archive_review_candidate_count": payload.get("archive_review_candidate_count"),
        "referenced_raw_script_count": payload.get("referenced_raw_script_count"),
        "reference_summary": payload.get("reference_summary"),
        "missing_qd_target_scripts": payload.get("missing_qd_target_scripts"),
        "risk_sections": payload.get("risk_sections"),
        "warnings": payload.get("warnings"),
        "output_paths": payload.get("output_paths"),
        "disclaimer": payload.get("disclaimer"),
    }


def dumps_payload(payload: dict[str, Any], *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
