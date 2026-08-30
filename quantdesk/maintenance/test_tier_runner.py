from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.maintenance.test_inventory import build_test_inventory
from quantdesk.shared import write_json, write_md
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "maintenance" / "test_tier_runner_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "maintenance" / "test_tier_runner_latest.md"
LEGACY_TIER = "legacy_versioned_acceptance"
VALID_TIERS = {"tier0_fast_unit", "tier1_focused_domain", "tier2_cli_or_filesystem", "tier3_release_or_long", LEGACY_TIER}
TIER_ALIASES = {
    "tier0": "tier0_fast_unit",
    "tier1": "tier1_focused_domain",
    "tier2": "tier2_cli_or_filesystem",
    "tier3": "tier3_release_or_long",
    "legacy": LEGACY_TIER,
}
DISCLAIMER = (
    "Bounded test tier runner. It only runs selected tests from inventory; "
    "it does not rewrite tests, enable network providers, connect brokers, or place real orders."
)


@dataclass(frozen=True)
class SelectedTestFile:
    path: str
    module: str
    tier: str
    test_count: int
    line_count: int
    categories: list[str]
    runner: str


def _rel(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    target = Path(path)
    try:
        return target.relative_to(root).as_posix()
    except Exception:
        return str(target).replace("\\", "/")


def _tail(text: str, limit: int = 4000) -> str:
    return str(text or "")[-limit:]


def module_from_path(path: str) -> str:
    return Path(path).with_suffix("").as_posix().replace("/", ".")


def normalize_tier(tier: str) -> str:
    value = str(tier or "tier0_fast_unit").strip()
    return TIER_ALIASES.get(value, value)


def select_test_files(
    *,
    tier: str = "tier0_fast_unit",
    category: str = "",
    limit: int = 40,
    include_tier3: bool = False,
    include_legacy: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> tuple[list[SelectedTestFile], dict[str, Any]]:
    tier = normalize_tier(tier)
    inventory = build_test_inventory(project_root)
    records = list(inventory.get("records") or [])
    if tier not in VALID_TIERS:
        raise ValueError(f"unsupported tier: {tier}")
    if tier == "tier3_release_or_long" and not include_tier3:
        return [], inventory
    if tier == LEGACY_TIER and not include_legacy:
        return [], inventory
    selected: list[SelectedTestFile] = []
    for record in sorted(records, key=lambda item: str(item.get("path") or "")):
        categories = list(record.get("categories") or [])
        if record.get("tier") != tier:
            continue
        if category and category not in categories:
            continue
        selected.append(
            SelectedTestFile(
                path=str(record.get("path") or ""),
                module=module_from_path(str(record.get("path") or "")),
                tier=str(record.get("tier") or ""),
                test_count=int(record.get("test_count") or 0),
                line_count=int(record.get("line_count") or 0),
                categories=categories,
                runner=str(record.get("runner") or "unittest"),
            )
        )
        if limit > 0 and len(selected) >= limit:
            break
    return selected, inventory


def _runner_commands(pytest_paths: list[str], unittest_modules: list[str]) -> tuple[str, list[dict[str, Any]]]:
    commands: list[dict[str, Any]] = []
    if pytest_paths:
        commands.append({"kind": "pytest", "command": [sys.executable, "-m", "pytest", *pytest_paths, "-q"]})
    if unittest_modules:
        commands.append({"kind": "unittest", "command": [sys.executable, "-m", "unittest", *unittest_modules, "-v"]})
    if len(commands) > 1:
        return "mixed", commands
    if commands:
        return str(commands[0]["kind"]), commands
    return "none", commands


def _run_runner_commands(
    commands: list[dict[str, Any]],
    *,
    timeout_seconds: int,
    project_root: Path,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    processes: list[dict[str, Any]] = []
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    status = "passed"
    for item in commands:
        command = list(item.get("command") or [])
        remaining = max(1.0, deadline - time.monotonic())
        started = datetime.now()
        try:
            proc = subprocess.run(
                command,
                cwd=project_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=remaining,
                check=False,
            )
            duration = (datetime.now() - started).total_seconds()
            process = {
                "kind": item.get("kind"),
                "executed": True,
                "returncode": proc.returncode,
                "duration_seconds": round(duration, 3),
                "stdout_tail": _tail(proc.stdout),
                "stderr_tail": _tail(proc.stderr),
            }
            processes.append(process)
            if proc.returncode != 0:
                status = "failed"
                break
        except subprocess.TimeoutExpired as exc:
            duration = (datetime.now() - started).total_seconds()
            processes.append(
                {
                    "kind": item.get("kind"),
                    "executed": True,
                    "returncode": None,
                    "duration_seconds": round(duration, 3),
                    "stdout_tail": _tail(exc.stdout or ""),
                    "stderr_tail": _tail(exc.stderr or ""),
                }
            )
            status = "timeout"
            warnings.append("runner_timeout")
            break
    aggregate = {
        "executed": bool(processes),
        "returncode": 0 if status == "passed" and processes else None,
        "duration_seconds": round(sum(float(item.get("duration_seconds") or 0.0) for item in processes), 3),
        "stdout_tail": "\n".join(str(item.get("stdout_tail") or "") for item in processes)[-4000:],
        "stderr_tail": "\n".join(str(item.get("stderr_tail") or "") for item in processes)[-4000:],
    }
    if status == "failed":
        failed = next((item for item in processes if item.get("returncode") not in {0, None}), None)
        aggregate["returncode"] = failed.get("returncode") if failed else 1
    return status, aggregate, processes, warnings


def build_test_tier_runner_payload(
    *,
    tier: str = "tier0_fast_unit",
    category: str = "",
    limit: int = 40,
    timeout_seconds: int = 180,
    dry_run: bool = True,
    include_tier3: bool = False,
    include_legacy: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    tier = normalize_tier(tier)
    selected, inventory = select_test_files(
        tier=tier,
        category=category,
        limit=limit,
        include_tier3=include_tier3,
        include_legacy=include_legacy,
        project_root=project_root,
    )
    pytest_paths = [item.path for item in selected if item.runner == "pytest"]
    unittest_modules = [item.module for item in selected if item.runner != "pytest"]
    command_kind, commands = _runner_commands(pytest_paths, unittest_modules)
    command = list(commands[0]["command"]) if len(commands) == 1 else []
    warnings: list[str] = []
    if tier == "tier3_release_or_long" and not include_tier3:
        warnings.append("tier3_requires_include_tier3_true")
    if tier == LEGACY_TIER and not include_legacy:
        warnings.append("legacy_tier_requires_include_legacy_true")
    if not selected:
        warnings.append("no_tests_selected")
    if len(selected) == limit and limit > 0:
        warnings.append("selection_limited")
    proc_payload: dict[str, Any] = {
        "executed": False,
        "returncode": None,
        "duration_seconds": 0.0,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    processes: list[dict[str, Any]] = []
    status = "planned"
    if commands and not dry_run:
        status, proc_payload, processes, run_warnings = _run_runner_commands(
            commands,
            timeout_seconds=timeout_seconds,
            project_root=project_root,
        )
        warnings.extend(run_warnings)
    payload: dict[str, Any] = {
        "ok": status in {"planned", "passed"},
        "status": "warning" if warnings and status in {"planned", "passed"} else status,
        "task_type": "test_tier_runner",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "tier": tier,
        "category": category,
        "limit": int(limit),
        "timeout_seconds": int(timeout_seconds),
        "dry_run": bool(dry_run),
        "include_tier3": bool(include_tier3),
        "include_legacy": bool(include_legacy),
        "test_file_count": inventory.get("test_file_count"),
        "test_method_count": inventory.get("test_method_count"),
        "counts_by_tier": inventory.get("counts_by_tier"),
        "selected_file_count": len(selected),
        "selected_test_count": sum(item.test_count for item in selected),
        "selected_files": [asdict(item) for item in selected],
        "command": command,
        "commands": commands,
        "command_kind": command_kind,
        "pytest_paths": pytest_paths,
        "unittest_modules": unittest_modules,
        "process": proc_payload,
        "processes": processes,
        "warnings": warnings,
        "warning_count": len(warnings),
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return payload


def render_test_tier_runner_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Test Tier Runner",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- tier: `{payload.get('tier')}`",
        f"- category: `{payload.get('category') or 'all'}`",
        f"- dry_run: `{payload.get('dry_run')}`",
        f"- selected_file_count: `{payload.get('selected_file_count')}`",
        f"- selected_test_count: `{payload.get('selected_test_count')}`",
        f"- executed: `{(payload.get('process') or {}).get('executed')}`",
        f"- command_kind: `{payload.get('command_kind')}`",
        "",
        "## Command",
        "",
    ]
    commands = payload.get("commands") or []
    if commands:
        for item in commands:
            lines.append(f"- `{item.get('kind')}`: `" + " ".join(item.get("command") or []) + "`")
    else:
        lines.append("`no tests selected`")
    lines.extend([
        "",
        "## Selected Files",
        "",
        "| path | tests | tier | categories |",
        "| --- | ---: | --- | --- |",
    ])
    for item in payload.get("selected_files") or []:
        lines.append(
            f"| `{item.get('path')}` | {item.get('test_count')} | `{item.get('tier')}` | "
            f"{', '.join(item.get('categories') or [])} |"
        )
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in payload.get("warnings") or []] if payload.get("warnings") else ["- none"])
    return "\n".join(lines) + "\n"


def write_test_tier_runner_outputs(
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
    write_md(report, render_test_tier_runner_markdown(payload))
    return payload


def compact_test_tier_runner_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "task_type": payload.get("task_type"),
        "tier": payload.get("tier"),
        "category": payload.get("category"),
        "dry_run": payload.get("dry_run"),
        "include_legacy": payload.get("include_legacy"),
        "selected_file_count": payload.get("selected_file_count"),
        "selected_test_count": payload.get("selected_test_count"),
        "command": payload.get("command"),
        "commands": payload.get("commands"),
        "command_kind": payload.get("command_kind"),
        "process": payload.get("process"),
        "processes": payload.get("processes"),
        "warnings": payload.get("warnings"),
        "output_paths": payload.get("output_paths"),
        "disclaimer": payload.get("disclaimer"),
    }


def dumps_payload(payload: dict[str, Any], *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
