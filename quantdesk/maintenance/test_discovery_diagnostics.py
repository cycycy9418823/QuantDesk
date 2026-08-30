from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import unittest
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.maintenance.test_inventory import build_test_inventory
from quantdesk.shared import write_json, write_md
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "maintenance" / "test_discovery_diagnostics_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "maintenance" / "test_discovery_diagnostics_latest.md"
DISCLAIMER = (
    "Read-only unittest discovery diagnostic. It imports/discovers tests with subprocess timeouts, "
    "but it does not delete, skip, deselect, or rewrite tests."
)


@dataclass(frozen=True)
class DiscoveryProbe:
    path: str
    tier: str
    test_count: int
    line_count: int
    status: str
    duration_seconds: float
    returncode: int | None
    stdout_tail: str
    stderr_tail: str
    command: list[str]
    categories: list[str]


@dataclass(frozen=True)
class TimedTestProbe:
    path: str
    tier: str
    test_count: int
    line_count: int
    timeout_seconds: float
    status: str
    duration_seconds: float
    returncode: int | None
    started_test_count: int
    completed_test_count: int
    timed_out_test_id: str
    slowest_tests: list[dict[str, Any]]
    command: list[str]
    stdout_tail: str
    stderr_tail: str
    categories: list[str]


TIMING_START_PREFIX = "QD_TIMING_START "
TIMING_DONE_PREFIX = "QD_TIMING_DONE "
TIMING_SUMMARY_PREFIX = "QD_TIMING_SUMMARY "


def _rel(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    target = Path(path)
    try:
        return target.relative_to(root).as_posix()
    except Exception:
        return str(target).replace("\\", "/")


def _tail(text: str, limit: int = 1200) -> str:
    text = str(text or "")
    return text[-limit:]


def _probe_command(path: str) -> list[str]:
    return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", Path(path).name]


def _timed_test_probe_command(path: str) -> list[str]:
    return [sys.executable, "scripts/run_unittest_timing_probe.py", "--pattern", Path(path).name]


def _effective_timed_test_timeout_seconds(record: dict[str, Any], requested_timeout_seconds: float) -> float:
    """Give long release-tier files enough budget to prove aggregate completion."""

    requested = max(1.0, float(requested_timeout_seconds or 0.0))
    tier = str(record.get("tier") or "")
    test_count = int(record.get("test_count") or 0)
    line_count = int(record.get("line_count") or 0)
    categories = {str(item) for item in record.get("categories") or []}
    if tier == "tier3_release_or_long" or "subprocess" in categories or test_count >= 120 or line_count >= 1800:
        return max(requested, 45.0)
    return requested


def _probe_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _record_is_legacy(record: dict[str, Any]) -> bool:
    return str(record.get("tier") or "") == "legacy_versioned_acceptance"


def _filtered_discovery_records(inventory: dict[str, Any], *, include_legacy: bool) -> list[dict[str, Any]]:
    records = list(inventory.get("records") or [])
    if include_legacy:
        return records
    return [record for record in records if not _record_is_legacy(record)]


def _discover_filtered_unittest_cases(*, include_legacy: bool = False) -> dict[str, Any]:
    """Import/count unittest-style tests without pulling legacy snapshots by default."""

    inventory = build_test_inventory()
    records = _filtered_discovery_records(inventory, include_legacy=include_legacy)
    loader = unittest.defaultTestLoader
    imported = 0
    failures: list[str] = []
    total_cases = 0
    for record in records:
        path = str(record.get("path") or "")
        if not path:
            continue
        module_name = Path(path).with_suffix("").as_posix().replace("/", ".")
        try:
            importlib.import_module(module_name)
            imported += 1
            total_cases += loader.loadTestsFromName(module_name).countTestCases()
        except Exception as exc:
            failures.append(f"{path}: {type(exc).__name__}: {exc}")
    return {
        "include_legacy": bool(include_legacy),
        "module_count": len(records),
        "imported_module_count": imported,
        "unittest_case_count": total_cases,
        "failure_count": len(failures),
        "failures": failures[:20],
    }


def _discover_import_probe_main(include_legacy: bool = False) -> int:
    payload = _discover_filtered_unittest_cases(include_legacy=include_legacy)
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 1 if payload["failure_count"] else 0


def _probe_review(probe: DiscoveryProbe) -> dict[str, Any]:
    review = {
        "path": probe.path,
        "status": probe.status,
        "tier": probe.tier,
        "test_count": probe.test_count,
        "line_count": probe.line_count,
        "duration_seconds": probe.duration_seconds,
        "categories": probe.categories,
        "suspected_source": "",
        "next_action": "",
    }
    if probe.status == "timeout":
        review["suspected_source"] = "per_file_runtime_exceeds_timeout"
        review["next_action"] = (
            "Keep this file in tier3/release validation or split expensive integration checks into smaller targeted files."
        )
    elif probe.status == "failed":
        stderr = f"{probe.stderr_tail}\n{probe.stdout_tail}"
        if "ModuleNotFoundError" in stderr:
            review["suspected_source"] = "import_path_assumption"
            review["next_action"] = "Run through unittest discover semantics or fix sibling imports without deleting coverage."
        else:
            review["suspected_source"] = "per_file_test_failure"
            review["next_action"] = "Run the single-file discover command from command[] and repair the reported failure."
    else:
        review["suspected_source"] = "none"
        review["next_action"] = "No action required for this probe."
    return review


def _timed_probe_review(probe: TimedTestProbe) -> dict[str, Any]:
    review = {
        "path": probe.path,
        "status": probe.status,
        "tier": probe.tier,
        "test_count": probe.test_count,
        "line_count": probe.line_count,
        "duration_seconds": probe.duration_seconds,
        "timeout_seconds": probe.timeout_seconds,
        "started_test_count": probe.started_test_count,
        "completed_test_count": probe.completed_test_count,
        "timed_out_test_id": probe.timed_out_test_id,
        "slowest_tests": probe.slowest_tests[:10],
        "categories": probe.categories,
        "suspected_source": "",
        "next_action": "",
    }
    if probe.status == "timeout":
        nearly_complete = probe.completed_test_count > 0 and probe.completed_test_count >= max(0, probe.started_test_count - 1)
        if probe.timed_out_test_id and not nearly_complete:
            review["suspected_source"] = "method_runtime_exceeds_timed_probe_timeout"
            review["next_action"] = (
                "Run the timed_out_test_id directly, then split or cache the expensive setup without removing coverage."
            )
        elif probe.timed_out_test_id and nearly_complete:
            review["suspected_source"] = "aggregate_runtime_exceeds_timed_probe_budget"
            review["next_action"] = (
                "Do not blame the last started method alone; split the large file or cache repeated expensive setup."
            )
        elif probe.started_test_count == 0:
            review["suspected_source"] = "import_or_setUpClass_before_first_test"
            review["next_action"] = "Inspect module import and setUpClass work before the first test method starts."
        else:
            review["suspected_source"] = "aggregate_runtime_many_tests"
            review["next_action"] = "Use slowest_tests to split this file into focused tier2/tier3 suites."
    elif probe.status == "failed":
        review["suspected_source"] = "timed_probe_test_failure"
        review["next_action"] = "Run the command[] from the probe and repair the reported failure before timing cleanup."
    else:
        review["suspected_source"] = "aggregate_runtime_many_tests" if probe.completed_test_count else "none"
        review["next_action"] = "Use slowest_tests as split candidates; no failing method was found in the timed probe."
    return review


def _probe_module(path: str, record: dict[str, Any], *, timeout_seconds: float) -> DiscoveryProbe:
    command = _probe_command(path)
    started = datetime.now()
    try:
        proc = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=_probe_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        duration = (datetime.now() - started).total_seconds()
        status = "passed" if proc.returncode == 0 else "failed"
        return DiscoveryProbe(
            path=path,
            tier=str(record.get("tier") or "unknown"),
            test_count=int(record.get("test_count") or 0),
            line_count=int(record.get("line_count") or 0),
            status=status,
            duration_seconds=round(duration, 3),
            returncode=proc.returncode,
            stdout_tail=_tail(proc.stdout),
            stderr_tail=_tail(proc.stderr),
            command=command,
            categories=list(record.get("categories") or []),
        )
    except subprocess.TimeoutExpired as exc:
        duration = (datetime.now() - started).total_seconds()
        return DiscoveryProbe(
            path=path,
            tier=str(record.get("tier") or "unknown"),
            test_count=int(record.get("test_count") or 0),
            line_count=int(record.get("line_count") or 0),
            status="timeout",
            duration_seconds=round(duration, 3),
            returncode=None,
            stdout_tail=_tail(exc.stdout or ""),
            stderr_tail=_tail(exc.stderr or ""),
            command=command,
            categories=list(record.get("categories") or []),
        )


def _load_marker_payload(line: str, prefix: str) -> dict[str, Any]:
    if not line.startswith(prefix):
        return {}
    raw = line[len(prefix) :].strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _parse_timed_test_output(text: str) -> dict[str, Any]:
    started: list[str] = []
    completed: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for line in str(text or "").splitlines():
        start_payload = _load_marker_payload(line, TIMING_START_PREFIX)
        if start_payload:
            test_id = str(start_payload.get("id") or "")
            if test_id:
                started.append(test_id)
            continue
        done_payload = _load_marker_payload(line, TIMING_DONE_PREFIX)
        if done_payload:
            test_id = str(done_payload.get("id") or "")
            if test_id:
                completed.append(
                    {
                        "id": test_id,
                        "seconds": round(float(done_payload.get("seconds") or 0.0), 3),
                    }
                )
            continue
        summary_payload = _load_marker_payload(line, TIMING_SUMMARY_PREFIX)
        if summary_payload:
            summary = summary_payload

    completed_ids = {str(item.get("id") or "") for item in completed}
    timed_out_test_id = ""
    for test_id in reversed(started):
        if test_id not in completed_ids:
            timed_out_test_id = test_id
            break
    slowest_tests = sorted(completed, key=lambda item: (-float(item.get("seconds") or 0.0), str(item.get("id") or "")))[:20]
    return {
        "started_test_count": len(started),
        "completed_test_count": len(completed),
        "timed_out_test_id": timed_out_test_id,
        "slowest_tests": slowest_tests,
        "summary": summary,
    }


def _probe_timed_tests(path: str, record: dict[str, Any], *, timeout_seconds: float) -> TimedTestProbe:
    command = _timed_test_probe_command(path)
    effective_timeout_seconds = _effective_timed_test_timeout_seconds(record, timeout_seconds)
    started_at = datetime.now()
    try:
        proc = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=_probe_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=effective_timeout_seconds,
            check=False,
        )
        duration = (datetime.now() - started_at).total_seconds()
        parsed = _parse_timed_test_output(proc.stdout)
        status = "passed" if proc.returncode == 0 else "failed"
        return TimedTestProbe(
            path=path,
            tier=str(record.get("tier") or "unknown"),
            test_count=int(record.get("test_count") or 0),
            line_count=int(record.get("line_count") or 0),
            timeout_seconds=round(effective_timeout_seconds, 3),
            status=status,
            duration_seconds=round(duration, 3),
            returncode=proc.returncode,
            started_test_count=int(parsed.get("started_test_count") or 0),
            completed_test_count=int(parsed.get("completed_test_count") or 0),
            timed_out_test_id=str(parsed.get("timed_out_test_id") or ""),
            slowest_tests=list(parsed.get("slowest_tests") or []),
            command=command,
            stdout_tail=_tail(proc.stdout),
            stderr_tail=_tail(proc.stderr),
            categories=list(record.get("categories") or []),
        )
    except subprocess.TimeoutExpired as exc:
        duration = (datetime.now() - started_at).total_seconds()
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="ignore")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="ignore")
        parsed = _parse_timed_test_output(stdout)
        return TimedTestProbe(
            path=path,
            tier=str(record.get("tier") or "unknown"),
            test_count=int(record.get("test_count") or 0),
            line_count=int(record.get("line_count") or 0),
            timeout_seconds=round(effective_timeout_seconds, 3),
            status="timeout",
            duration_seconds=round(duration, 3),
            returncode=None,
            started_test_count=int(parsed.get("started_test_count") or 0),
            completed_test_count=int(parsed.get("completed_test_count") or 0),
            timed_out_test_id=str(parsed.get("timed_out_test_id") or ""),
            slowest_tests=list(parsed.get("slowest_tests") or []),
            command=command,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            categories=list(record.get("categories") or []),
        )


def _probe_discover_import_only(*, timeout_seconds: float, include_legacy: bool = False) -> dict[str, Any]:
    code = (
        "from quantdesk.maintenance.test_discovery_diagnostics import _discover_import_probe_main; "
        f"raise SystemExit(_discover_import_probe_main(include_legacy={bool(include_legacy)!r}))"
    )
    command = [sys.executable, "-c", code]
    started = datetime.now()
    try:
        proc = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=_probe_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        duration = (datetime.now() - started).total_seconds()
        return {
            "status": "passed" if proc.returncode == 0 else "failed",
            "duration_seconds": round(duration, 3),
            "returncode": proc.returncode,
            "stdout_tail": _tail(proc.stdout),
            "stderr_tail": _tail(proc.stderr),
            "command": command,
        }
    except subprocess.TimeoutExpired as exc:
        duration = (datetime.now() - started).total_seconds()
        return {
            "status": "timeout",
            "duration_seconds": round(duration, 3),
            "returncode": None,
            "stdout_tail": _tail(exc.stdout or ""),
            "stderr_tail": _tail(exc.stderr or ""),
            "command": command,
        }


def _candidate_paths(inventory: dict[str, Any], *, sample_limit: int, include_legacy: bool = False) -> list[str]:
    records = _filtered_discovery_records(inventory, include_legacy=include_legacy)
    priority = sorted(
        records,
        key=lambda item: (
            str(item.get("tier") or "") != "tier3_release_or_long",
            -int(item.get("test_count") or 0),
            -int(item.get("line_count") or 0),
            str(item.get("path") or ""),
        ),
    )
    paths: list[str] = []
    for record in priority:
        path = str(record.get("path") or "")
        if path and path not in paths:
            paths.append(path)
        if len(paths) >= sample_limit:
            break
    return paths


def build_test_discovery_diagnostics(
    *,
    sample_limit: int = 12,
    per_file_timeout_seconds: float = 8.0,
    discover_timeout_seconds: float = 45.0,
    timed_test_timeout_seconds: float = 24.0,
    include_probes: bool = True,
    include_timed_test_probes: bool = True,
    include_legacy: bool = False,
) -> dict[str, Any]:
    inventory = build_test_inventory()
    import_probe = _probe_discover_import_only(
        timeout_seconds=discover_timeout_seconds,
        include_legacy=include_legacy,
    )
    candidate_paths = _candidate_paths(
        inventory,
        sample_limit=sample_limit,
        include_legacy=include_legacy,
    )
    by_path = {str(record.get("path")): record for record in inventory.get("records", [])}
    probes = [
        _probe_module(path, by_path.get(path, {}), timeout_seconds=per_file_timeout_seconds)
        for path in candidate_paths
    ] if include_probes else []
    timeout_paths = [probe.path for probe in probes if probe.status == "timeout"]
    failed_paths = [probe.path for probe in probes if probe.status == "failed"]
    slowest = sorted(probes, key=lambda item: (-item.duration_seconds, item.path))[:20]
    timeout_reviews = [_probe_review(probe) for probe in probes if probe.status == "timeout"]
    failed_reviews = [_probe_review(probe) for probe in probes if probe.status == "failed"]
    timed_test_probes = (
        [
            _probe_timed_tests(path, by_path.get(path, {}), timeout_seconds=timed_test_timeout_seconds)
            for path in timeout_paths
        ]
        if include_probes and include_timed_test_probes and timeout_paths and timed_test_timeout_seconds > 0
        else []
    )
    timed_test_probe_reviews = [_timed_probe_review(probe) for probe in timed_test_probes]
    timed_by_path = {probe.path: probe for probe in timed_test_probes}
    resolved_timeout_paths = [
        path
        for path in timeout_paths
        if timed_by_path.get(path) is not None and timed_by_path[path].status == "passed"
    ]
    unresolved_timeout_paths = [
        path
        for path in timeout_paths
        if path not in set(resolved_timeout_paths)
    ]
    warnings: list[str] = []
    if import_probe.get("status") != "passed":
        warnings.append("unittest_discover_import_probe_not_green")
    if unresolved_timeout_paths:
        warnings.append("per_file_probe_timeouts_present")
    if failed_paths:
        warnings.append("per_file_probe_failures_present")
    if [probe for probe in timed_test_probes if probe.status == "timeout"]:
        warnings.append("timed_test_probe_timeouts_present")
    if [probe for probe in timed_test_probes if probe.status == "failed"]:
        warnings.append("timed_test_probe_failures_present")
    tier_counts = inventory.get("counts_by_tier") or {}
    payload: dict[str, Any] = {
        "ok": import_probe.get("status") == "passed",
        "status": "warning" if warnings else "passed",
        "task_type": "test_discovery_diagnostics",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "discovery_scope": "unittest_import_probe_priority_per_file_and_timed_method_probes",
        "sample_limit": int(sample_limit),
        "per_file_timeout_seconds": float(per_file_timeout_seconds),
        "discover_timeout_seconds": float(discover_timeout_seconds),
        "timed_test_timeout_seconds": float(timed_test_timeout_seconds),
        "include_legacy": bool(include_legacy),
        "test_file_count": inventory.get("test_file_count"),
        "test_method_count": inventory.get("test_method_count"),
        "counts_by_tier": tier_counts,
        "import_discovery_probe": import_probe,
        "probe_count": len(probes),
        "timeout_count": len(timeout_paths),
        "resolved_timeout_count": len(resolved_timeout_paths),
        "unresolved_timeout_count": len(unresolved_timeout_paths),
        "failed_probe_count": len(failed_paths),
        "timeout_paths": timeout_paths,
        "resolved_timeout_paths": resolved_timeout_paths,
        "unresolved_timeout_paths": unresolved_timeout_paths,
        "failed_probe_paths": failed_paths,
        "timeout_probe_reviews": timeout_reviews,
        "failed_probe_reviews": failed_reviews,
        "timed_test_probe_count": len(timed_test_probes),
        "timed_test_probe_reviews": timed_test_probe_reviews,
        "timed_test_probes": [asdict(item) for item in timed_test_probes],
        "slowest_probes": [asdict(item) for item in slowest],
        "probes": [asdict(item) for item in probes],
        "recommendations": [
            "Default discovery diagnostics exclude legacy test_v* acceptance snapshots; add --include-legacy true for archival sweeps.",
            "If import_discovery_probe times out or fails, fix import-time side effects before running the selected suite.",
            "If only per-file probes time out, keep broad discover as release/nightly and run targeted tier suites locally.",
            "Use timed_test_probe_reviews to identify the method or aggregate group that consumes the timeout budget.",
            "Use test-inventory to select tier0/tier1/tier2/tier3 tests; do not delete valuable tests to reduce runtime.",
            "Network, broker, and real-order boundaries remain locked during diagnostics.",
        ],
        "warnings": warnings,
        "warning_count": len(warnings),
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return payload


def render_test_discovery_diagnostics_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Test Discovery Diagnostics",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- test_file_count: `{payload.get('test_file_count')}`",
        f"- test_method_count: `{payload.get('test_method_count')}`",
        f"- probe_count: `{payload.get('probe_count')}`",
        f"- timeout_count: `{payload.get('timeout_count')}`",
        f"- unresolved_timeout_count: `{payload.get('unresolved_timeout_count')}`",
        f"- resolved_timeout_count: `{payload.get('resolved_timeout_count')}`",
        f"- failed_probe_count: `{payload.get('failed_probe_count')}`",
        f"- timed_test_probe_count: `{payload.get('timed_test_probe_count')}`",
        "",
        "## Import Discovery Probe",
        "",
    ]
    probe = payload.get("import_discovery_probe") or {}
    lines.extend(
        [
            f"- status: `{probe.get('status')}`",
            f"- duration_seconds: `{probe.get('duration_seconds')}`",
            f"- returncode: `{probe.get('returncode')}`",
        ]
    )
    lines.extend(["", "## Slowest Probes", "", "| path | status | seconds | tests | tier |", "| --- | --- | ---: | ---: | --- |"])
    for item in payload.get("slowest_probes") or []:
        lines.append(
            f"| `{item.get('path')}` | `{item.get('status')}` | {item.get('duration_seconds')} | "
            f"{item.get('test_count')} | `{item.get('tier')}` |"
        )
    lines.extend(["", "## Timeout Paths", ""])
    lines.extend([f"- `{path}`" for path in payload.get("timeout_paths") or []] or ["- none"])
    lines.extend(["", "## Unresolved Timeout Paths", ""])
    lines.extend([f"- `{path}`" for path in payload.get("unresolved_timeout_paths") or []] or ["- none"])
    lines.extend(["", "## Resolved Timeout Paths", ""])
    lines.extend([f"- `{path}`" for path in payload.get("resolved_timeout_paths") or []] or ["- none"])
    lines.extend(["", "## Timeout Probe Reviews", ""])
    timeout_reviews = payload.get("timeout_probe_reviews") or []
    if timeout_reviews:
        for item in timeout_reviews:
            lines.append(
                f"- `{item.get('path')}` source=`{item.get('suspected_source')}` "
                f"next=`{item.get('next_action')}`"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Timed Test Probe Reviews", ""])
    timed_reviews = payload.get("timed_test_probe_reviews") or []
    if timed_reviews:
        lines.extend(
            [
                "| path | status | started | completed | timed_out_test_id | slowest_completed |",
                "| --- | --- | ---: | ---: | --- | --- |",
            ]
        )
        for item in timed_reviews:
            slowest = item.get("slowest_tests") or []
            slowest_label = ""
            if slowest:
                first = slowest[0]
                slowest_label = f"{first.get('id')} ({first.get('seconds')}s)"
            lines.append(
                f"| `{item.get('path')}` | `{item.get('status')}` | {item.get('started_test_count')} | "
                f"{item.get('completed_test_count')} | `{item.get('timed_out_test_id') or ''}` | `{slowest_label}` |"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Failed Probe Paths", ""])
    lines.extend([f"- `{path}`" for path in payload.get("failed_probe_paths") or []] or ["- none"])
    lines.extend(["", "## Failed Probe Reviews", ""])
    failed_reviews = payload.get("failed_probe_reviews") or []
    if failed_reviews:
        for item in failed_reviews:
            lines.append(
                f"- `{item.get('path')}` source=`{item.get('suspected_source')}` "
                f"next=`{item.get('next_action')}`"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Recommendations", ""])
    lines.extend([f"- {item}" for item in payload.get("recommendations") or []] or ["- none"])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in payload.get("warnings") or []] if payload.get("warnings") else ["- none"])
    return "\n".join(lines) + "\n"


def write_test_discovery_diagnostics_outputs(
    payload: dict[str, Any] | None = None,
    *,
    result_path: str | Path = RESULT_JSON,
    report_path: str | Path = REPORT_MD,
) -> dict[str, Any]:
    payload = dict(payload or build_test_discovery_diagnostics())
    result = Path(result_path)
    report = Path(report_path)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    write_json(result, payload)
    write_md(report, render_test_discovery_diagnostics_markdown(payload))
    return payload


def compact_test_discovery_diagnostics_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "task_type": payload.get("task_type"),
        "test_file_count": payload.get("test_file_count"),
        "test_method_count": payload.get("test_method_count"),
        "counts_by_tier": payload.get("counts_by_tier"),
        "import_discovery_probe": payload.get("import_discovery_probe"),
        "probe_count": payload.get("probe_count"),
        "timeout_count": payload.get("timeout_count"),
        "resolved_timeout_count": payload.get("resolved_timeout_count"),
        "unresolved_timeout_count": payload.get("unresolved_timeout_count"),
        "failed_probe_count": payload.get("failed_probe_count"),
        "timeout_paths": payload.get("timeout_paths"),
        "resolved_timeout_paths": payload.get("resolved_timeout_paths"),
        "unresolved_timeout_paths": payload.get("unresolved_timeout_paths"),
        "failed_probe_paths": payload.get("failed_probe_paths"),
        "timeout_probe_reviews": payload.get("timeout_probe_reviews"),
        "failed_probe_reviews": payload.get("failed_probe_reviews"),
        "timed_test_probe_count": payload.get("timed_test_probe_count"),
        "timed_test_probe_reviews": payload.get("timed_test_probe_reviews"),
        "include_legacy": payload.get("include_legacy"),
        "warnings": payload.get("warnings"),
        "output_paths": payload.get("output_paths"),
        "disclaimer": payload.get("disclaimer"),
    }


def dumps_payload(payload: dict[str, Any], *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose unittest discovery/import hot spots without modifying tests.")
    parser.add_argument("--sample-limit", type=int, default=12)
    parser.add_argument("--per-file-timeout-seconds", type=float, default=8.0)
    parser.add_argument("--discover-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--timed-test-timeout-seconds", type=float, default=24.0)
    parser.add_argument("--no-probes", action="store_true")
    parser.add_argument("--no-timed-test-probes", action="store_true")
    parser.add_argument("--include-legacy", default="false")
    return parser.parse_args(argv)
