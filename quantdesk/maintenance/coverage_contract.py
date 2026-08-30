from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import write_json, write_md
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "maintenance" / "coverage_contract_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "maintenance" / "coverage_contract_latest.md"
COVERAGE_XML_REL = Path("reports") / "coverage" / "coverage.xml"
COVERAGE_XML = PROJECT_ROOT / COVERAGE_XML_REL
DEFAULT_TEST_MODULES = [
    "tests/test_frontend_api_contract_governance.py",
    "tests/test_dependency_contract_governance.py",
    "tests/test_data_backtest_contract_governance.py",
    "tests/test_gitlab_ci_governance.py",
]
DISCLAIMER = (
    "Bounded coverage contract for maintenance governance tests. It does not fetch data, enable network providers, "
    "connect brokers, place real orders, or claim full-suite coverage."
)


def _rel(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    target = Path(path)
    if not target.is_absolute():
        target = root / target
    try:
        return str(target.relative_to(root)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _tail(text: str, limit: int = 4000) -> str:
    return str(text or "")[-limit:]


def parse_coverage_xml(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"exists": False, "line_rate": None, "branch_rate": None, "lines_valid": 0, "lines_covered": 0}
    try:
        root = ET.parse(target).getroot()
    except Exception as exc:  # noqa: BLE001
        return {"exists": True, "parse_error": str(exc), "line_rate": None, "branch_rate": None, "lines_valid": 0, "lines_covered": 0}
    line_rate = float(root.attrib.get("line-rate", 0.0) or 0.0)
    branch_rate = float(root.attrib.get("branch-rate", 0.0) or 0.0)
    lines_valid = int(float(root.attrib.get("lines-valid", 0) or 0))
    lines_covered = int(float(root.attrib.get("lines-covered", 0) or 0))
    return {
        "exists": True,
        "line_rate": line_rate,
        "line_percent": round(line_rate * 100, 2),
        "branch_rate": branch_rate,
        "branch_percent": round(branch_rate * 100, 2),
        "lines_valid": lines_valid,
        "lines_covered": lines_covered,
    }


def build_coverage_contract(
    *,
    test_modules: list[str] | None = None,
    timeout_seconds: int = 180,
    dry_run: bool = False,
    min_line_percent: float = 0.0,
    require_pytest_cov: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    root = Path(project_root)
    tests = list(test_modules or DEFAULT_TEST_MODULES)
    coverage_xml = root / COVERAGE_XML_REL
    command = [
        sys.executable,
        "-m",
        "pytest",
        *tests,
        "--cov=quantdesk/maintenance",
        f"--cov-report=xml:{_rel(coverage_xml, root=root)}",
        "--cov-report=term",
        "-q",
    ]
    warnings: list[str] = []
    pytest_cov_available = importlib.util.find_spec("pytest_cov") is not None
    process: dict[str, Any] = {
        "executed": False,
        "returncode": None,
        "duration_seconds": 0.0,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    status = "planned" if dry_run else "passed"
    if dry_run:
        warnings.append("dry_run_no_coverage_generated")
    elif not pytest_cov_available:
        warnings.append("pytest_cov_missing_install_requirements_dev")
        status = "failed" if require_pytest_cov else "warning"
    else:
        coverage_xml.parent.mkdir(parents=True, exist_ok=True)
        started = datetime.now()
        try:
            proc = subprocess.run(
                command,
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(1, int(timeout_seconds)),
                check=False,
            )
            duration = (datetime.now() - started).total_seconds()
            process = {
                "executed": True,
                "returncode": proc.returncode,
                "duration_seconds": round(duration, 3),
                "stdout_tail": _tail(proc.stdout),
                "stderr_tail": _tail(proc.stderr),
            }
            status = "passed" if proc.returncode == 0 else "failed"
        except subprocess.TimeoutExpired as exc:
            duration = (datetime.now() - started).total_seconds()
            process = {
                "executed": True,
                "returncode": None,
                "duration_seconds": round(duration, 3),
                "stdout_tail": _tail(exc.stdout or ""),
                "stderr_tail": _tail(exc.stderr or ""),
            }
            status = "timeout"
            warnings.append("coverage_command_timeout")
    coverage = parse_coverage_xml(coverage_xml)
    line_percent = coverage.get("line_percent")
    if line_percent is None:
        if not dry_run:
            warnings.append("coverage_xml_missing_or_unreadable")
    elif float(line_percent) < float(min_line_percent):
        warnings.append("coverage_below_min_line_percent")
        if status == "passed":
            status = "warning"
    payload: dict[str, Any] = {
        "ok": status in {"planned", "passed", "warning"},
        "status": "warning" if warnings and status in {"planned", "passed"} else status,
        "task_type": "coverage_contract",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "test_modules": tests,
        "test_module_count": len(tests),
        "command": command,
        "timeout_seconds": int(timeout_seconds),
        "dry_run": bool(dry_run),
        "min_line_percent": float(min_line_percent),
        "require_pytest_cov": bool(require_pytest_cov),
        "pytest_cov_available": bool(pytest_cov_available),
        "coverage": coverage,
        "coverage_xml": _rel(coverage_xml, root=root),
        "process": process,
        "warnings": warnings,
        "warning_count": len(warnings),
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return payload


def render_coverage_contract_markdown(payload: dict[str, Any]) -> str:
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    lines = [
        "# Coverage Contract",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- dry_run: `{payload.get('dry_run')}`",
        f"- require_pytest_cov: `{payload.get('require_pytest_cov')}`",
        f"- pytest_cov_available: `{payload.get('pytest_cov_available')}`",
        f"- test_module_count: `{payload.get('test_module_count')}`",
        f"- line_percent: `{coverage.get('line_percent')}`",
        f"- branch_percent: `{coverage.get('branch_percent')}`",
        f"- lines_covered: `{coverage.get('lines_covered')}` / `{coverage.get('lines_valid')}`",
        f"- coverage_xml: `{payload.get('coverage_xml')}`",
        "",
        "## Command",
        "",
        "`" + " ".join(str(item) for item in (payload.get("command") or [])) + "`",
        "",
        "## Test Modules",
        "",
    ]
    lines.extend([f"- `{item}`" for item in payload.get("test_modules") or []] or ["- none"])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in payload.get("warnings") or []] if payload.get("warnings") else ["- none"])
    return "\n".join(lines) + "\n"


def write_coverage_contract_outputs(
    payload: dict[str, Any],
    *,
    result_path: str | Path = RESULT_JSON,
    report_path: str | Path = REPORT_MD,
) -> dict[str, Any]:
    payload = dict(payload)
    result = Path(result_path)
    report = Path(report_path)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report), "coverage_xml": payload.get("coverage_xml")}
    write_json(result, payload)
    write_md(report, render_coverage_contract_markdown(payload))
    return payload


def compact_coverage_contract_summary(payload: dict[str, Any]) -> dict[str, Any]:
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    return {
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "task_type": payload.get("task_type"),
        "dry_run": payload.get("dry_run"),
        "require_pytest_cov": payload.get("require_pytest_cov"),
        "pytest_cov_available": payload.get("pytest_cov_available"),
        "test_module_count": payload.get("test_module_count"),
        "line_percent": coverage.get("line_percent"),
        "branch_percent": coverage.get("branch_percent"),
        "lines_covered": coverage.get("lines_covered"),
        "lines_valid": coverage.get("lines_valid"),
        "process": payload.get("process"),
        "warnings": payload.get("warnings"),
        "output_paths": payload.get("output_paths"),
        "disclaimer": payload.get("disclaimer"),
    }


def dumps_payload(payload: dict[str, Any], *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
