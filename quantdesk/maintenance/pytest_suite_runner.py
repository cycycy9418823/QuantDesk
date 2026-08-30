from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import write_json, write_md
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "maintenance" / "pytest_suite_runner_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "maintenance" / "pytest_suite_runner_latest.md"

SUITE_ORDER = ("core", "api", "research", "v3_v4_governance", "slow")
VALID_SUITES = set(SUITE_ORDER) | {"all"}
SUITE_ALIASES = {
    "v3-v4": "v3_v4_governance",
    "v3_v4": "v3_v4_governance",
    "governance": "v3_v4_governance",
    "v3v4": "v3_v4_governance",
}
DISCLAIMER = (
    "Bounded pytest suite runner. It splits the broad pytest surface into core, api, "
    "research, v3_v4_governance, and slow suites. It does not rewrite tests, enable "
    "network providers, connect brokers, place real orders, or generate release zips."
)


@dataclass(frozen=True)
class PytestSuiteFile:
    path: str
    suite: str
    line_count: int
    test_count: int
    reason: str


def _rel(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    target = Path(path)
    try:
        return target.relative_to(root).as_posix()
    except Exception:
        return str(target).replace("\\", "/")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _tail(text: str, limit: int = 6000) -> str:
    return str(text or "")[-limit:]


def _test_count(text: str) -> int:
    return len(re.findall(r"(?m)^\s*(?:async\s+)?def\s+test_", text))


def normalize_suite(suite: str) -> str:
    value = str(suite or "core").strip().lower().replace("-", "_")
    return SUITE_ALIASES.get(value, value)


def _classify_test_file(path: Path, text: str) -> tuple[str, str]:
    name = path.name.lower()
    lowered = text.lower()
    line_count = len(text.splitlines())

    if re.match(r"test_(?:v\d+|rc\d*)", name) or any(
        token in name
        for token in (
            "governance",
            "script_inventory",
            "dependency_contract",
            "frontend_api_contract",
            "coverage_contract",
            "data_backtest_contract",
            "test_inventory",
            "test_discovery",
            "test_tier_runner",
            "pytest_suite_runner",
            "gitlab_ci",
        )
    ):
        return "v3_v4_governance", "versioned/governance acceptance surface"

    if line_count > 1200 or any(
        token in lowered
        for token in (
            "subprocess.run",
            "export_project",
            "clean_zip",
            "verify_clean_zip",
            "release packaging",
            "run_tests.cmd",
            "time.sleep(",
        )
    ):
        return "slow", "large file, subprocess, release, or long-running test risk"

    api_name_tokens = (
        "api",
        "daily_research",
        "dashboard",
        "frontend",
        "server",
        "route",
        "theme_map",
        "news",
        "spot",
    )
    research_name_tokens = (
        "research",
        "factor",
        "backtest",
        "walk_forward",
        "candidate",
        "screen",
        "strategy",
        "paper",
        "portfolio",
        "market",
        "data",
        "metadata",
        "provider",
        "tushare",
        "delist",
        "enriched",
        "signal",
    )
    if any(token in name for token in api_name_tokens):
        return "api", "active 8000 API, dashboard, or frontend contract"

    if any(token in name for token in research_name_tokens):
        return "research", "research, data, strategy, paper, or provider domain"

    if any(token in lowered for token in ("flask", "test_client", "/api/", "quant_api_server")):
        return "api", "active 8000 API, dashboard, or frontend contract"

    if any(token in lowered for token in research_name_tokens):
        return "research", "research, data, strategy, paper, or provider domain"

    return "core", "small core unit or infrastructure test"


def discover_pytest_suite_files(project_root: Path = PROJECT_ROOT) -> list[PytestSuiteFile]:
    tests_dir = Path(project_root) / "tests"
    files: list[PytestSuiteFile] = []
    if not tests_dir.exists():
        return files
    for path in sorted(tests_dir.glob("test_*.py")):
        text = _read_text(path)
        suite, reason = _classify_test_file(path, text)
        files.append(
            PytestSuiteFile(
                path=_rel(path, root=project_root),
                suite=suite,
                line_count=len(text.splitlines()),
                test_count=_test_count(text),
                reason=reason,
            )
        )
    return files


def selected_suite_files(
    suite: str,
    *,
    limit: int = 0,
    project_root: Path = PROJECT_ROOT,
) -> list[PytestSuiteFile]:
    suite = normalize_suite(suite)
    if suite not in VALID_SUITES:
        raise ValueError(f"unsupported pytest suite: {suite}")
    files = discover_pytest_suite_files(project_root)
    selected = [item for item in files if suite == "all" or item.suite == suite]
    if suite == "all":
        selected = sorted(selected, key=lambda item: (SUITE_ORDER.index(item.suite), item.path))
    if limit > 0:
        selected = selected[:limit]
    return selected


def _command(paths: list[str], *, maxfail: int) -> list[str]:
    return [sys.executable, "-m", "pytest", *paths, "-q", "--tb=short", "--maxfail", str(max(1, int(maxfail)))]


def _parse_red_points(stdout: str, stderr: str) -> list[str]:
    text = "\n".join([stdout or "", stderr or ""])
    red: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("FAILED tests/", "ERROR tests/")):
            red.append(stripped.split(" - ", 1)[0])
    return red[:80]


def run_one_suite(
    suite: str,
    *,
    limit: int = 0,
    timeout_seconds: int = 180,
    maxfail: int = 10,
    dry_run: bool = True,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    suite = normalize_suite(suite)
    selected = selected_suite_files(suite, limit=limit, project_root=project_root)
    paths = [item.path for item in selected]
    command = _command(paths, maxfail=maxfail) if paths else []
    status = "planned"
    process: dict[str, Any] = {
        "executed": False,
        "returncode": None,
        "duration_seconds": 0.0,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    red_points: list[str] = []
    warnings: list[str] = []
    if not paths:
        warnings.append("no_tests_selected")
    if paths and not dry_run:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                cwd=project_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(1, int(timeout_seconds)),
                check=False,
            )
            duration = time.monotonic() - started
            status = "passed" if proc.returncode == 0 else "failed"
            process = {
                "executed": True,
                "returncode": proc.returncode,
                "duration_seconds": round(duration, 3),
                "stdout_tail": _tail(proc.stdout),
                "stderr_tail": _tail(proc.stderr),
            }
            red_points = _parse_red_points(proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            status = "timeout"
            warnings.append("suite_timeout")
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            process = {
                "executed": True,
                "returncode": None,
                "duration_seconds": round(duration, 3),
                "stdout_tail": _tail(stdout),
                "stderr_tail": _tail(stderr),
            }
            red_points = _parse_red_points(stdout, stderr)
    return {
        "suite": suite,
        "status": "warning" if status in {"planned", "passed"} and warnings else status,
        "ok": status in {"planned", "passed"},
        "dry_run": bool(dry_run),
        "limit": int(limit),
        "timeout_seconds": int(timeout_seconds),
        "maxfail": int(maxfail),
        "selected_file_count": len(selected),
        "selected_test_count": sum(item.test_count for item in selected),
        "selected_files": [asdict(item) for item in selected],
        "pytest_paths": paths,
        "command": command,
        "process": process,
        "red_points": red_points,
        "red_point_count": len(red_points),
        "warnings": warnings,
    }


def build_pytest_suite_runner_payload(
    *,
    suite: str = "core",
    limit: int = 0,
    timeout_seconds: int = 180,
    maxfail: int = 10,
    dry_run: bool = True,
    stop_on_failure: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    suite = normalize_suite(suite)
    suites = list(SUITE_ORDER) if suite == "all" else [suite]
    if suite not in VALID_SUITES:
        raise ValueError(f"unsupported pytest suite: {suite}")

    all_files = discover_pytest_suite_files(project_root)
    counts_by_suite = {key: 0 for key in SUITE_ORDER}
    tests_by_suite = {key: 0 for key in SUITE_ORDER}
    for item in all_files:
        counts_by_suite[item.suite] += 1
        tests_by_suite[item.suite] += item.test_count

    suite_runs: list[dict[str, Any]] = []
    for item in suites:
        run = run_one_suite(
            item,
            limit=limit,
            timeout_seconds=timeout_seconds,
            maxfail=maxfail,
            dry_run=dry_run,
            project_root=project_root,
        )
        suite_runs.append(run)
        if stop_on_failure and run.get("status") in {"failed", "timeout"}:
            break

    failed = [item for item in suite_runs if item.get("status") == "failed"]
    timed_out = [item for item in suite_runs if item.get("status") == "timeout"]
    warnings = [f"{item.get('suite')}: {warning}" for item in suite_runs for warning in item.get("warnings", [])]
    status = "passed"
    if dry_run:
        status = "planned"
    if failed:
        status = "failed"
    if timed_out:
        status = "timeout" if not failed else "failed_with_timeouts"
    if warnings and status in {"planned", "passed"}:
        status = "warning"
    payload: dict[str, Any] = {
        "ok": not failed and not timed_out,
        "status": status,
        "task_type": "pytest_suite_runner",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "suite": suite,
        "available_suites": list(SUITE_ORDER),
        "limit": int(limit),
        "timeout_seconds": int(timeout_seconds),
        "maxfail": int(maxfail),
        "dry_run": bool(dry_run),
        "stop_on_failure": bool(stop_on_failure),
        "test_file_count": len(all_files),
        "test_method_count": sum(item.test_count for item in all_files),
        "counts_by_suite": counts_by_suite,
        "tests_by_suite": tests_by_suite,
        "suite_runs": suite_runs,
        "red_points": [
            {"suite": item.get("suite"), "red_points": item.get("red_points", [])}
            for item in suite_runs
            if item.get("red_points")
        ],
        "warning_count": len(warnings),
        "warnings": warnings,
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return payload


def render_pytest_suite_runner_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Pytest Suite Runner",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- suite: `{payload.get('suite')}`",
        f"- dry_run: `{payload.get('dry_run')}`",
        f"- timeout_seconds_per_suite: `{payload.get('timeout_seconds')}`",
        f"- maxfail: `{payload.get('maxfail')}`",
        "",
        "## Counts By Suite",
        "",
        "| suite | files | tests |",
        "| --- | ---: | ---: |",
    ]
    counts = payload.get("counts_by_suite") or {}
    tests = payload.get("tests_by_suite") or {}
    for suite in SUITE_ORDER:
        lines.append(f"| `{suite}` | {counts.get(suite, 0)} | {tests.get(suite, 0)} |")
    lines.extend(["", "## Runs", "", "| suite | status | files | tests | seconds | red points |", "| --- | --- | ---: | ---: | ---: | ---: |"])
    for run in payload.get("suite_runs") or []:
        process = run.get("process") or {}
        lines.append(
            f"| `{run.get('suite')}` | `{run.get('status')}` | {run.get('selected_file_count')} | "
            f"{run.get('selected_test_count')} | {process.get('duration_seconds')} | {run.get('red_point_count')} |"
        )
    lines.extend(["", "## Red Points", ""])
    red = payload.get("red_points") or []
    if red:
        for suite_red in red:
            lines.append(f"### {suite_red.get('suite')}")
            lines.extend([f"- `{item}`" for item in suite_red.get("red_points") or []])
            lines.append("")
    else:
        lines.append("- none")
    lines.extend(["", "## Commands", ""])
    for run in payload.get("suite_runs") or []:
        command = run.get("command") or []
        lines.append(f"- `{run.get('suite')}`: `" + " ".join(command or ["no tests selected"]) + "`")
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in payload.get("warnings") or []] if payload.get("warnings") else ["- none"])
    return "\n".join(lines) + "\n"


def write_pytest_suite_runner_outputs(
    payload: dict[str, Any],
    *,
    result_path: str | Path = RESULT_JSON,
    report_path: str | Path = REPORT_MD,
) -> dict[str, Any]:
    payload = dict(payload)
    result = Path(result_path)
    report = Path(report_path)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    write_json(result, payload)
    write_md(report, render_pytest_suite_runner_markdown(payload))
    return payload


def compact_pytest_suite_runner_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "task_type": payload.get("task_type"),
        "suite": payload.get("suite"),
        "dry_run": payload.get("dry_run"),
        "counts_by_suite": payload.get("counts_by_suite"),
        "tests_by_suite": payload.get("tests_by_suite"),
        "suite_runs": [
            {
                "suite": item.get("suite"),
                "status": item.get("status"),
                "selected_file_count": item.get("selected_file_count"),
                "selected_test_count": item.get("selected_test_count"),
                "duration_seconds": (item.get("process") or {}).get("duration_seconds"),
                "red_points": item.get("red_points"),
                "warnings": item.get("warnings"),
            }
            for item in payload.get("suite_runs") or []
        ],
        "warnings": payload.get("warnings"),
        "output_paths": payload.get("output_paths"),
        "disclaimer": payload.get("disclaimer"),
    }


def dumps_payload(payload: dict[str, Any], *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
