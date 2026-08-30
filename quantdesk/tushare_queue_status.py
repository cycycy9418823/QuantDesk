"""Read-only status reporting for local Tushare continuous download queues.

This module never fetches network data, never reads provider tokens directly,
and never mutates staging/canonical market data.  It only inspects local queue
state and log metadata so operators can decide whether to keep waiting, audit
failures, or explicitly approve a restart.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from quantdesk.shared import read_json, write_json, write_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "tushare_queue_status_latest.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "tushare_queue_status_latest.md"

QUEUE_GLOB = "data/vendor_staging/tushare_continuous_queue_*"
LOG_COUNTERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ok_count", re.compile(r"^OK\b", re.IGNORECASE)),
    ("fail_count", re.compile(r"^FAIL\b", re.IGNORECASE)),
    ("rate_limit_count", re.compile(r"\b(429|rate[_ -]?limit|requests-per-minute|rpm)\b", re.IGNORECASE)),
    ("ssl_eof_count", re.compile(r"(UNEXPECTED_EOF|EOF occurred|SSL.*EOF)", re.IGNORECASE)),
    ("permission_count", re.compile(r"(permission|权限|积分|没有权限|denied|forbidden)", re.IGNORECASE)),
    ("traceback_count", re.compile(r"(Traceback \(most recent call last\)|FATAL ERROR|Exception:)", re.IGNORECASE)),
    ("timeout_count", re.compile(r"\b(timeout|timed out)\b", re.IGNORECASE)),
)
PROCESS_SCRIPT_HINTS = (
    "tushare_continuous_data_queue.py",
    "tushare_proxy_extended_harvest.py",
    "tushare_minute_probe_and_download.py",
)


@dataclass(frozen=True)
class LogSnapshot:
    path: Path
    stream: str
    stage: str
    size_bytes: int
    modified_at: str
    age_seconds: float | None
    counters: dict[str, int]
    tail: list[str]


def _root(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _latest_queue(root: Path) -> Path | None:
    queues = [path for path in root.glob(QUEUE_GLOB) if path.is_dir()]
    if not queues:
        return None
    return sorted(queues, key=lambda item: (item.name, item.stat().st_mtime), reverse=True)[0]


def _redact_text(text: str) -> str:
    redacted = str(text)
    redacted = re.sub(r"([?&]token=)[^&\s]+", r"\1<redacted-token>", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"\b(token|api[_-]?key|apikey)\s*[:=]\s*[^,\s]+", r"\1=<redacted-token>", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"\b[a-fA-F0-9]{32,}\b", "<redacted-token-like>", redacted)
    return redacted


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _tail_lines(path: Path, *, limit: int = 8) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except Exception:
        return []
    return [_redact_text(line) for line in lines[-limit:]]


def _count_patterns(path: Path) -> dict[str, int]:
    counters = {name: 0 for name, _pattern in LOG_COUNTERS}
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                for name, pattern in LOG_COUNTERS:
                    if pattern.search(line):
                        counters[name] += 1
    except Exception:
        pass
    return counters


def _stage_from_log_name(path: Path) -> tuple[str, str]:
    name = path.name
    if name.endswith("_stdout.log"):
        return name[: -len("_stdout.log")], "stdout"
    if name.endswith("_stderr.log"):
        return name[: -len("_stderr.log")], "stderr"
    if name.endswith(".log"):
        return name[: -len(".log")], "log"
    return path.stem, "unknown"


def _iter_log_files(queue: Path) -> list[Path]:
    candidates: list[Path] = []
    for base in (queue / "logs", queue):
        if base.exists():
            candidates.extend(path for path in base.glob("*.log") if path.is_file())
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _log_snapshot(path: Path, root: Path, now: datetime) -> dict[str, Any]:
    stage, stream = _stage_from_log_name(path)
    try:
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        age_seconds: float | None = max(0.0, (now - modified).total_seconds())
        size_bytes = int(stat.st_size)
        modified_at = modified.isoformat()
    except Exception:
        age_seconds = None
        size_bytes = 0
        modified_at = ""
    snapshot = LogSnapshot(
        path=path,
        stream=stream,
        stage=stage,
        size_bytes=size_bytes,
        modified_at=modified_at,
        age_seconds=age_seconds,
        counters=_count_patterns(path),
        tail=_tail_lines(path),
    )
    return {
        "path": _relative(snapshot.path, root),
        "stage": snapshot.stage,
        "stream": snapshot.stream,
        "size_bytes": snapshot.size_bytes,
        "modified_at": snapshot.modified_at,
        "age_seconds": snapshot.age_seconds,
        "counters": snapshot.counters,
        "tail": snapshot.tail,
    }


def _sum_counters(logs: Iterable[dict[str, Any]]) -> dict[str, int]:
    totals = {name: 0 for name, _pattern in LOG_COUNTERS}
    for item in logs:
        counters = item.get("counters")
        if not isinstance(counters, dict):
            continue
        for key in totals:
            try:
                totals[key] += int(counters.get(key) or 0)
            except Exception:
                pass
    return totals


def _is_self_process_query(command_line: str) -> bool:
    lowered = command_line.lower()
    return "get-ciminstance win32_process" in lowered and (
        "tushare_proxy_extended_harvest.py" in lowered
        or "tushare_minute_probe_and_download.py" in lowered
        or "tushare_proxy_minutes" in lowered
        or "tushare_continuous_data_queue.py" in lowered
    )


def _process_counts() -> dict[str, Any]:
    counts = {hint: 0 for hint in PROCESS_SCRIPT_HINTS}
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'tushare' } | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must not fail the command
        return {"checked": False, "counts": counts, "error": _redact_text(str(exc))}
    if completed.returncode != 0:
        return {"checked": False, "counts": counts, "error": _redact_text(completed.stderr.strip())}
    if not completed.stdout.strip():
        return {"checked": True, "counts": counts, "error": ""}
    try:
        parsed = json.loads(completed.stdout)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not fail the command
        return {"checked": False, "counts": counts, "error": _redact_text(str(exc))}
    rows = parsed if isinstance(parsed, list) else [parsed]
    for row in rows:
        if not isinstance(row, dict):
            continue
        command_line = _redact_text(str(row.get("CommandLine") or ""))
        if _is_self_process_query(command_line):
            continue
        lowered = command_line.lower()
        for hint in PROCESS_SCRIPT_HINTS:
            if hint.lower() in lowered:
                counts[hint] += 1
    return {"checked": True, "counts": counts, "error": ""}


def _choose_current_logs(logs: list[dict[str, Any]], current_stage: str) -> dict[str, Any]:
    by_stream: dict[str, dict[str, Any]] = {}
    candidates = [
        item
        for item in logs
        if current_stage
        and (
            str(item.get("stage") or "") == current_stage
            or str(item.get("stage") or "").startswith(f"{current_stage}_")
        )
    ]
    if not candidates:
        candidates = logs
    for item in candidates:
        stream = str(item.get("stream") or "unknown")
        if stream not in by_stream:
            by_stream[stream] = item
    return by_stream


def _derive_current_stage(status_payload: dict[str, Any]) -> tuple[str, str]:
    current_stage = str(status_payload.get("current_stage") or status_payload.get("stage") or "").strip()
    current_stage_status = str(status_payload.get("current_stage_status") or "").strip()
    stages = status_payload.get("stages")
    if current_stage:
        return current_stage, current_stage_status
    if isinstance(stages, list):
        for item in reversed(stages):
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or item.get("stage") or item.get("name") or "").strip()
            if not key:
                continue
            return key, str(item.get("status") or "").strip()
    return "", current_stage_status


def _requested_stage_items(status_payload: dict[str, Any]) -> list[dict[str, str]]:
    """Return requested queue stages as small, safe, ordered records."""

    raw_requested = status_payload.get("stages_requested")
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    if isinstance(raw_requested, list):
        for raw in raw_requested:
            if isinstance(raw, dict):
                key = str(raw.get("key") or raw.get("stage") or raw.get("name") or "").strip()
                label = str(raw.get("label") or raw.get("title") or "").strip()
            else:
                key = str(raw or "").strip()
                label = ""
            if not key or key in seen:
                continue
            seen.add(key)
            items.append({"key": key, "label": label})

    if items:
        return items

    raw_stages = status_payload.get("stages")
    if isinstance(raw_stages, list):
        for raw in raw_stages:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or raw.get("stage") or raw.get("name") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append({"key": key, "label": ""})
    return items


def _latest_stage_statuses(status_payload: dict[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    raw_stages = status_payload.get("stages")
    if isinstance(raw_stages, list):
        for raw in raw_stages:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or raw.get("stage") or raw.get("name") or "").strip()
            if not key:
                continue
            statuses[key] = str(raw.get("status") or "").strip()
    return statuses


def _parse_stage_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _stage_event_age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _parse_stage_datetime(value)
    if parsed is None:
        return None
    return max(0.0, (now.astimezone(parsed.tzinfo) - parsed).total_seconds())


def _stage_timing(status_payload: dict[str, Any], *, current_stage: str, now: datetime) -> dict[str, Any]:
    raw_stages = status_payload.get("stages")
    events = [item for item in raw_stages if isinstance(item, dict)] if isinstance(raw_stages, list) else []
    last_event = events[-1] if events else {}
    current_events = [
        item
        for item in events
        if str(item.get("key") or item.get("stage") or item.get("name") or "").strip() == current_stage
    ]
    current_start = next(
        (
            str(item.get("updated_at") or "").strip()
            for item in current_events
            if str(item.get("updated_at") or "").strip()
        ),
        "",
    )
    current_last = current_events[-1] if current_events else {}
    current_last_at = str(current_last.get("updated_at") or "").strip()
    current_last_status = str(current_last.get("status") or "").strip()
    last_event_at = str(last_event.get("updated_at") or "").strip()
    last_event_key = str(last_event.get("key") or last_event.get("stage") or last_event.get("name") or "").strip()
    last_event_status = str(last_event.get("status") or "").strip()

    current_start_dt = _parse_stage_datetime(current_start)
    current_last_dt = _parse_stage_datetime(current_last_at)
    current_elapsed: float | None = None
    if current_start_dt is not None:
        end_dt = current_last_dt if current_last_dt is not None and _is_stage_complete(current_last_status) else now
        current_elapsed = max(0.0, (end_dt.astimezone(current_start_dt.tzinfo) - current_start_dt).total_seconds())

    return {
        "current_stage_started_at": current_start,
        "current_stage_elapsed_seconds": current_elapsed,
        "current_stage_last_event_at": current_last_at,
        "current_stage_last_event_status": current_last_status,
        "current_stage_last_event_age_seconds": _stage_event_age_seconds(current_last_at, now),
        "last_stage_event_at": last_event_at,
        "last_stage_event_key": last_event_key,
        "last_stage_event_status": last_event_status,
        "last_stage_event_age_seconds": _stage_event_age_seconds(last_event_at, now),
    }


def _is_stage_complete(status: str) -> bool:
    normalized = str(status or "").strip().lower()
    if not normalized:
        return False
    return normalized in {"completed", "complete", "done", "finished", "success", "command_finished"} or normalized.startswith("skipped")


def _stage_progress(
    *,
    status_payload: dict[str, Any],
    queue_status: str,
    current_stage: str,
    current_stage_status: str,
    now: datetime,
) -> dict[str, Any]:
    """Build operator-facing queue stage progress without exposing raw status blobs."""

    requested_items = _requested_stage_items(status_payload)
    requested_keys = [item["key"] for item in requested_items]
    labels = {item["key"]: item.get("label") or "" for item in requested_items}
    statuses = _latest_stage_statuses(status_payload)
    if current_stage and current_stage_status:
        statuses[current_stage] = current_stage_status

    normalized_queue_status = str(queue_status or "").strip().lower()
    if normalized_queue_status in {"completed", "complete", "done", "finished", "success"}:
        for key in requested_keys:
            statuses.setdefault(key, "completed")

    current_index: int | None = None
    if current_stage and current_stage in requested_keys:
        current_index = requested_keys.index(current_stage)
    elif requested_keys:
        for index in range(len(requested_keys) - 1, -1, -1):
            if requested_keys[index] in statuses:
                current_index = index
                if not current_stage:
                    current_stage = requested_keys[index]
                    current_stage_status = statuses.get(current_stage, "")
                break

    completed_keys = [key for key in requested_keys if _is_stage_complete(statuses.get(key, ""))]
    if current_index is None:
        remaining_keys = [key for key in requested_keys if key not in completed_keys]
    else:
        remaining_keys = requested_keys[current_index + 1 :]

    current_stage_number = current_index + 1 if current_index is not None else None
    requested_count = len(requested_keys)
    stage_records = [
        {
            "key": key,
            "label": labels.get(key, ""),
            "position": index + 1,
            "status": statuses.get(key, ""),
            "is_current": key == current_stage,
            "is_complete": _is_stage_complete(statuses.get(key, "")),
        }
        for index, key in enumerate(requested_keys)
    ]

    return {
        "requested_stage_count": requested_count,
        "completed_stage_count": len(completed_keys),
        "current_stage": current_stage,
        "current_stage_status": current_stage_status,
        "current_stage_index": current_index,
        "current_stage_number": current_stage_number,
        "stage_position_label": f"{current_stage_number}/{requested_count}" if current_stage_number and requested_count else "",
        "remaining_stage_count": len(remaining_keys),
        "completed_stage_keys": completed_keys,
        "remaining_stage_keys": remaining_keys,
        "stages": stage_records,
        **_stage_timing(status_payload, current_stage=current_stage, now=now),
    }


def _suggest_action(
    *,
    queue_status: str,
    current_stage: str,
    process_info: dict[str, Any],
    current_logs: dict[str, Any],
    totals: dict[str, int],
    stale_after_seconds: int,
) -> str:
    normalized_status = queue_status.strip().lower()
    if normalized_status in {"completed", "complete", "done", "finished", "success"}:
        return "run_audit_extended_import"
    if totals.get("traceback_count", 0) > 0:
        return "inspect_logs_before_restart"

    stdout = current_logs.get("stdout")
    stdout_age = stdout.get("age_seconds") if isinstance(stdout, dict) else None
    process_counts = process_info.get("counts") if isinstance(process_info.get("counts"), dict) else {}
    has_queue_runner = int(process_counts.get("tushare_continuous_data_queue.py") or 0) > 0
    has_extended_harvest = int(process_counts.get("tushare_proxy_extended_harvest.py") or 0) > 0
    has_minute_harvest = int(process_counts.get("tushare_minute_probe_and_download.py") or 0) > 0
    stage_key = str(current_stage or "").strip().lower()
    if stage_key.startswith("minute_"):
        relevant_process = has_queue_runner or has_minute_harvest
    elif stage_key.startswith("harvest_") or stage_key in {"wait_extended_events", "audit_extended_events"}:
        relevant_process = has_queue_runner or has_extended_harvest
    else:
        relevant_process = has_queue_runner or has_extended_harvest or has_minute_harvest

    if isinstance(stdout_age, (int, float)) and stdout_age <= stale_after_seconds:
        return "continue_monitoring"
    if not relevant_process and isinstance(stdout_age, (int, float)) and stdout_age > stale_after_seconds:
        return "restart_queue_after_user_approval"
    if relevant_process:
        return "continue_monitoring"
    return "inspect_logs"


def build_tushare_queue_status(
    *,
    project_root: str | Path | None = None,
    include_processes: bool = True,
    stale_after_seconds: int = 900,
) -> dict[str, Any]:
    """Build a read-only status payload for the latest Tushare queue."""

    root = _root(project_root)
    now = _utc_now()
    queue = _latest_queue(root)
    if queue is None:
        return {
            "task_type": "tushare_queue_status",
            "status": "missing",
            "generated_at": now.isoformat(),
            "queue": {"path": "", "status": "missing", "current_stage": ""},
            "processes": {"checked": False, "counts": {}, "error": ""},
            "logs": [],
            "current_logs": {},
            "log_summary": {name: 0 for name, _pattern in LOG_COUNTERS},
            "stage_progress": {
                "requested_stage_count": 0,
                "completed_stage_count": 0,
                "current_stage": "",
                "current_stage_status": "",
                "current_stage_index": None,
                "current_stage_number": None,
                "stage_position_label": "",
                "remaining_stage_count": 0,
                "completed_stage_keys": [],
                "remaining_stage_keys": [],
                "stages": [],
            },
            "warnings": ["missing:tushare_queue"],
            "suggested_next_action": "no_queue_found",
            "research_only": True,
            "no_network_fetch": True,
            "no_broker": True,
        }

    raw_status = read_json(queue / "queue_status.json", {})
    status_payload = _sanitize_value(raw_status if isinstance(raw_status, dict) else {})
    queue_status = str(status_payload.get("status") or "unknown")
    current_stage, current_stage_status = _derive_current_stage(status_payload)
    stage_progress = _stage_progress(
        status_payload=status_payload,
        queue_status=queue_status,
        current_stage=current_stage,
        current_stage_status=current_stage_status,
        now=now,
    )
    log_payloads = [_log_snapshot(path, root, now) for path in _iter_log_files(queue)]
    current_logs = _choose_current_logs(log_payloads, current_stage)
    totals = _sum_counters(log_payloads)
    process_info = _process_counts() if include_processes else {"checked": False, "counts": {}, "error": ""}
    suggested = _suggest_action(
        queue_status=queue_status,
        current_stage=current_stage,
        process_info=process_info,
        current_logs=current_logs,
        totals=totals,
        stale_after_seconds=stale_after_seconds,
    )
    warnings: list[str] = []
    if totals.get("fail_count", 0) > 0:
        warnings.append(f"log_fail_count:{totals['fail_count']}")
    if totals.get("ssl_eof_count", 0) > 0:
        warnings.append(f"ssl_eof_count:{totals['ssl_eof_count']}")
    if totals.get("timeout_count", 0) > 0:
        warnings.append(f"timeout_count:{totals['timeout_count']}")
    if totals.get("traceback_count", 0) > 0:
        warnings.append(f"traceback_count:{totals['traceback_count']}")
    if not status_payload:
        warnings.append("missing:queue_status_json")

    return {
        "task_type": "tushare_queue_status",
        "status": queue_status,
        "generated_at": now.isoformat(),
        "queue": {
            "path": _relative(queue, root),
            "status": queue_status,
            "current_stage": current_stage,
            "current_stage_status": current_stage_status,
            "modified_at": _iso_from_timestamp(queue.stat().st_mtime),
            "stage_progress": stage_progress,
            "queue_status": status_payload,
        },
        "processes": process_info,
        "logs": log_payloads,
        "current_logs": current_logs,
        "log_summary": totals,
        "stage_progress": stage_progress,
        "warnings": warnings,
        "suggested_next_action": suggested,
        "research_only": True,
        "no_network_fetch": True,
        "no_broker": True,
    }


def render_tushare_queue_status_report(payload: dict[str, Any]) -> str:
    queue = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
    stage_progress = queue.get("stage_progress") if isinstance(queue.get("stage_progress"), dict) else {}
    summary = payload.get("log_summary") if isinstance(payload.get("log_summary"), dict) else {}
    current_logs = payload.get("current_logs") if isinstance(payload.get("current_logs"), dict) else {}
    processes = payload.get("processes") if isinstance(payload.get("processes"), dict) else {}
    lines = [
        "# Tushare Queue Status",
        "",
        "> Research Only / Local Queue Diagnostics / No Network Fetch / No Broker Route / Not Investment Advice",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Suggested next action: `{payload.get('suggested_next_action')}`",
        f"- Queue: `{queue.get('path') or '-'}`",
        f"- Current stage: `{queue.get('current_stage') or '-'}`",
        f"- Stage progress: `{stage_progress.get('stage_position_label') or '-'}`",
        f"- Remaining stages: `{stage_progress.get('remaining_stage_count', 0)}`",
        f"- Process check: `{processes.get('checked')}`",
        "",
        "## Log summary",
        "",
        f"- OK: {summary.get('ok_count', 0)}",
        f"- FAIL: {summary.get('fail_count', 0)}",
        f"- rate-limit: {summary.get('rate_limit_count', 0)}",
        f"- SSL EOF: {summary.get('ssl_eof_count', 0)}",
        f"- permission: {summary.get('permission_count', 0)}",
        f"- timeout: {summary.get('timeout_count', 0)}",
        f"- traceback: {summary.get('traceback_count', 0)}",
        "",
        "## Current log tails",
        "",
    ]
    for stream in ("stdout", "stderr"):
        item = current_logs.get(stream) if isinstance(current_logs.get(stream), dict) else {}
        lines.append(f"### {stream}")
        lines.append("")
        lines.append(f"- Path: `{item.get('path') or '-'}`")
        lines.append(f"- Modified at: `{item.get('modified_at') or '-'}`")
        lines.append(f"- Size bytes: `{item.get('size_bytes') or 0}`")
        lines.append("")
        lines.append("```")
        tail = item.get("tail") if isinstance(item.get("tail"), list) else []
        lines.extend(str(line) for line in tail[-8:])
        lines.append("```")
        lines.append("")
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    lines.append("## Warnings")
    lines.append("")
    if warnings:
        for warning in warnings:
            lines.append(f"- `{warning}`")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def write_tushare_queue_status_outputs(
    *,
    project_root: str | Path | None = None,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    include_processes: bool = True,
    stale_after_seconds: int = 900,
) -> dict[str, Any]:
    payload = build_tushare_queue_status(
        project_root=project_root,
        include_processes=include_processes,
        stale_after_seconds=stale_after_seconds,
    )
    output = Path(output_path)
    report = Path(report_path)
    payload["output_path"] = output.as_posix()
    payload["report_path"] = report.as_posix()
    write_json(output, payload)
    write_text(report, render_tushare_queue_status_report(payload))
    return payload
