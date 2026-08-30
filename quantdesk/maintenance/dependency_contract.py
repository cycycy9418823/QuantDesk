from __future__ import annotations

import ast
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import write_json, write_md
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "maintenance" / "dependency_contract_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "maintenance" / "dependency_contract_latest.md"
DISCLAIMER = (
    "Read-only dependency contract inventory. It compares pyproject.toml with requirements files; "
    "it does not install packages, call providers, enable network access, connect brokers, or place real orders."
)

REQUIREMENT_FILES = {
    "runtime": "requirements.txt",
    "dev": "requirements-dev.txt",
    "data": "requirements-data.txt",
}
DRIFT_WARNING_CODES = {
    "runtime": "runtime_requirements_drift",
    "dev": "dev_requirements_drift",
    "data": "data_requirements_drift",
}
OPTIONAL_GROUPS = {"dev", "data"}
OPTIONAL_ONLY_PACKAGES = {"akshare", "mootdx"}
DEV_ONLY_PACKAGES = {"pytest", "pytest-cov", "ruff", "mypy"}


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


def _clean_requirement_lines(path: Path) -> list[str]:
    text = _read_text(path)
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            lines.append(line)
    return lines


def _normalized_name(requirement: str) -> str:
    head = re.split(r"[<>=!~;\[]", str(requirement or ""), maxsplit=1)[0].strip()
    return head.lower().replace("_", "-")


def _extract_array_from_pyproject(text: str, key: str) -> list[str]:
    match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(\[.*?\])", text, flags=re.DOTALL)
    if not match:
        return []
    try:
        value = ast.literal_eval(match.group(1))
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _extract_optional_group(text: str, group: str) -> list[str]:
    in_optional = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_optional = stripped == "[project.optional-dependencies]"
            continue
        if not in_optional:
            continue
        if stripped.startswith(f"{group} "):
            return _extract_array_from_pyproject(stripped, group)
        if stripped.startswith(f"{group}="):
            return _extract_array_from_pyproject(stripped, group)
    return []


def _project_metadata(text: str) -> dict[str, Any]:
    name_match = re.search(r'(?m)^name\s*=\s*"([^"]+)"', text)
    python_match = re.search(r'(?m)^requires-python\s*=\s*"([^"]+)"', text)
    return {
        "name": name_match.group(1) if name_match else "",
        "requires_python": python_match.group(1) if python_match else "",
        "dynamic_version": 'dynamic = ["version"]' in text or "dynamic = ['version']" in text,
        "setuptools_dynamic_version": "quantdesk.version.PACKAGE_VERSION" in text,
    }


def _set_diff(left: list[str], right: list[str]) -> dict[str, list[str]]:
    left_set = set(left)
    right_set = set(right)
    return {
        "left_only": sorted(left_set - right_set),
        "right_only": sorted(right_set - left_set),
    }


def _name_diff(left: list[str], right: list[str]) -> dict[str, list[str]]:
    left_names = {_normalized_name(item) for item in left}
    right_names = {_normalized_name(item) for item in right}
    return {
        "left_only": sorted(left_names - right_names),
        "right_only": sorted(right_names - left_names),
    }


def build_dependency_contract(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    project_root = Path(project_root)
    pyproject_path = project_root / "pyproject.toml"
    pyproject_text = _read_text(pyproject_path)
    metadata = _project_metadata(pyproject_text)
    pyproject_groups = {
        "runtime": _extract_array_from_pyproject(pyproject_text, "dependencies"),
        "dev": _extract_optional_group(pyproject_text, "dev"),
        "data": _extract_optional_group(pyproject_text, "data"),
    }
    requirement_groups = {
        group: _clean_requirement_lines(project_root / filename)
        for group, filename in REQUIREMENT_FILES.items()
    }
    group_checks: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for group in ("runtime", "dev", "data"):
        exact_diff = _set_diff(pyproject_groups[group], requirement_groups[group])
        name_diff = _name_diff(pyproject_groups[group], requirement_groups[group])
        status = "passed" if not exact_diff["left_only"] and not exact_diff["right_only"] else "warning"
        if status != "passed":
            warnings.append(DRIFT_WARNING_CODES[group])
        group_checks[group] = {
            "status": status,
            "pyproject": pyproject_groups[group],
            "requirements_file": REQUIREMENT_FILES[group],
            "requirements": requirement_groups[group],
            "exact_diff": exact_diff,
            "name_diff": name_diff,
            "pyproject_count": len(pyproject_groups[group]),
            "requirements_count": len(requirement_groups[group]),
        }
    runtime_names = {_normalized_name(item) for item in requirement_groups["runtime"]}
    dev_names = {_normalized_name(item) for item in requirement_groups["dev"]}
    data_names = {_normalized_name(item) for item in requirement_groups["data"]}
    optional_leaks = sorted(OPTIONAL_ONLY_PACKAGES & runtime_names)
    dev_leaks = sorted(DEV_ONLY_PACKAGES & runtime_names)
    if optional_leaks:
        warnings.append("optional_data_dependencies_in_runtime")
    if dev_leaks:
        warnings.append("dev_dependencies_in_runtime")
    if not data_names >= OPTIONAL_ONLY_PACKAGES:
        warnings.append("optional_data_group_missing_expected_provider_dependencies")
    if not dev_names >= DEV_ONLY_PACKAGES:
        warnings.append("dev_group_missing_expected_tools")
    if not metadata.get("dynamic_version") or not metadata.get("setuptools_dynamic_version"):
        warnings.append("package_version_not_dynamic")
    if metadata.get("requires_python") != ">=3.10":
        warnings.append("requires_python_contract_changed")
    missing_files = [
        _rel(project_root / path)
        for path in ["pyproject.toml", *REQUIREMENT_FILES.values()]
        if not (project_root / path).exists()
    ]
    if missing_files:
        warnings.append("dependency_contract_files_missing")
    recommendations = [
        "Keep runtime dependencies in both pyproject.toml [project].dependencies and requirements.txt.",
        "Keep provider packages in requirements-data.txt and pyproject optional group data; do not move them into runtime.",
        "Keep test/lint/type-check tools in requirements-dev.txt and pyproject optional group dev.",
        "Run this check after dependency edits and before clean source export.",
    ]
    status = "warning" if warnings else "passed"
    payload: dict[str, Any] = {
        "ok": not missing_files,
        "status": status,
        "task_type": "dependency_contract",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "pyproject": _rel(pyproject_path, root=project_root),
        "metadata": metadata,
        "required_files": ["pyproject.toml", *REQUIREMENT_FILES.values()],
        "missing_files": missing_files,
        "groups": group_checks,
        "runtime_dependency_count": len(requirement_groups["runtime"]),
        "dev_dependency_count": len(requirement_groups["dev"]),
        "data_dependency_count": len(requirement_groups["data"]),
        "optional_data_packages_in_runtime": optional_leaks,
        "dev_packages_in_runtime": dev_leaks,
        "optional_data_packages": sorted(data_names),
        "dev_packages": sorted(dev_names),
        "warnings": warnings,
        "warning_count": len(warnings),
        "recommendations": recommendations,
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return payload


def render_dependency_contract_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Dependency Contract",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- requires_python: `{(payload.get('metadata') or {}).get('requires_python')}`",
        f"- runtime_dependency_count: `{payload.get('runtime_dependency_count')}`",
        f"- dev_dependency_count: `{payload.get('dev_dependency_count')}`",
        f"- data_dependency_count: `{payload.get('data_dependency_count')}`",
        "",
        "## Groups",
        "",
        "| group | status | pyproject | requirements | diff |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for group, item in (payload.get("groups") or {}).items():
        diff = item.get("exact_diff") or {}
        diff_text = f"pyproject_only={diff.get('left_only') or []}; requirements_only={diff.get('right_only') or []}"
        lines.append(
            f"| `{group}` | `{item.get('status')}` | {item.get('pyproject_count')} | "
            f"{item.get('requirements_count')} | {diff_text} |"
        )
    lines.extend(["", "## Recommendations", ""])
    lines.extend([f"- {item}" for item in payload.get("recommendations") or []] or ["- none"])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in payload.get("warnings") or []] if payload.get("warnings") else ["- none"])
    return "\n".join(lines) + "\n"


def write_dependency_contract_outputs(
    payload: dict[str, Any] | None = None,
    *,
    result_path: str | Path = RESULT_JSON,
    report_path: str | Path = REPORT_MD,
) -> dict[str, Any]:
    payload = dict(payload or build_dependency_contract())
    result = Path(result_path)
    report = Path(report_path)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    write_json(result, payload)
    write_md(report, render_dependency_contract_markdown(payload))
    return payload


def compact_dependency_contract_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "task_type": payload.get("task_type"),
        "requires_python": (payload.get("metadata") or {}).get("requires_python"),
        "runtime_dependency_count": payload.get("runtime_dependency_count"),
        "dev_dependency_count": payload.get("dev_dependency_count"),
        "data_dependency_count": payload.get("data_dependency_count"),
        "group_statuses": {
            group: item.get("status")
            for group, item in (payload.get("groups") or {}).items()
        },
        "optional_data_packages_in_runtime": payload.get("optional_data_packages_in_runtime"),
        "dev_packages_in_runtime": payload.get("dev_packages_in_runtime"),
        "warnings": payload.get("warnings"),
        "output_paths": payload.get("output_paths"),
        "disclaimer": payload.get("disclaimer"),
    }


def dumps_payload(payload: dict[str, Any], *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
