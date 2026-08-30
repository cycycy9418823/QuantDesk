"""Read-only status for local Tushare 1-minute download jobs.

This module never fetches network data, never reads provider tokens, never
restarts processes, and never imports staged data.  It only inspects local
minute-download directories under ``data/vendor_staging`` so operators and
agents can tell whether a large minute job is waiting for token input, running,
stopped, rate-limited, or complete.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quantdesk.shared import read_json, write_json, write_text
from quantdesk.tushare_minute_quality import (
    load_latest_tushare_minute_quality_snapshot,
    summarize_completed_minute_quality_snapshot,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "tushare_minute_status_latest.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "tushare_minute_status_latest.md"

MINUTE_JOB_GLOB = "data/vendor_staging/tushare_proxy_minutes_250d*"
TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"([?&]token=)[^&\s]+", re.IGNORECASE),
    re.compile(r"\b(token|api[_-]?key|apikey)\s*[:=]\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"\b[a-fA-F0-9]{32,}\b"),
)
COUNTERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("probe_count", re.compile(r"\bPROBE\b", re.IGNORECASE)),
    ("ok_chunk_count", re.compile(r"\bOK\s+symbol=", re.IGNORECASE)),
    ("symbol_done_count", re.compile(r"\bSYMBOL_DONE\b", re.IGNORECASE)),
    ("completed_count", re.compile(r"\bCOMPLETED\b", re.IGNORECASE)),
    ("rate_limit_count", re.compile(r"\b(429|rate[_ -]?limit|requests-per-minute|rpm)\b", re.IGNORECASE)),
    (
        "permission_count",
        re.compile(
            r"(rate_or_permission=True|stopped_rate_or_permission|permission_denied|permission denied|"
            r"\bdenied\b|\bforbidden\b|\bquota\b|积分|权限|访问限制)",
            re.IGNORECASE,
        ),
    ),
    ("traceback_count", re.compile(r"(Traceback \(most recent call last\)|FATAL ERROR|Exception:)", re.IGNORECASE)),
)


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


def _redact_text(text: str) -> str:
    redacted = str(text)
    redacted = TOKEN_PATTERNS[0].sub(r"\1<redacted-token>", redacted)
    redacted = TOKEN_PATTERNS[1].sub(r"\1=<redacted-token>", redacted)
    redacted = TOKEN_PATTERNS[2].sub("<redacted-token-like>", redacted)
    return redacted


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _latest_job(root: Path) -> Path | None:
    jobs = [path for path in root.glob(MINUTE_JOB_GLOB) if path.is_dir()]
    if not jobs:
        return None
    return sorted(jobs, key=lambda item: (item.stat().st_mtime, item.name), reverse=True)[0]


def _tail_lines(path: Path, *, limit: int = 10) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except Exception:
        return []
    return [_redact_text(line) for line in lines[-limit:]]


def _count_patterns(path: Path) -> dict[str, int]:
    counters = {name: 0 for name, _pattern in COUNTERS}
    if not path.exists():
        return counters
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                for name, pattern in COUNTERS:
                    if pattern.search(line):
                        counters[name] += 1
    except Exception:
        pass
    return counters


def _latest_activity_timestamp(paths: list[Path], fallback: float) -> float:
    timestamps: list[float] = []
    for path in paths:
        try:
            if path.exists():
                timestamps.append(path.stat().st_mtime)
        except Exception:
            continue
    return max(timestamps) if timestamps else fallback


def _last_symbol_done(stdout_path: Path) -> dict[str, Any]:
    for line in reversed(_tail_lines(stdout_path, limit=500)):
        if "SYMBOL_DONE" not in line:
            continue
        match = re.search(r"SYMBOL_DONE\s+(\d+)/(\d+)\s+symbol=(\d{6})\s+rows=(\d+)\s+status=([^\s]+)", line)
        if not match:
            return {"line": line}
        return {
            "done_index": int(match.group(1)),
            "total": int(match.group(2)),
            "symbol": match.group(3),
            "rows": int(match.group(4)),
            "status": match.group(5),
            "line": line,
        }
    return {}


def _read_summary(summary_path: Path) -> dict[str, Any]:
    payload = read_json(summary_path, default={})
    if not isinstance(payload, dict):
        return {}
    downloads = payload.get("download") if isinstance(payload.get("download"), list) else []
    last_download = downloads[-1] if downloads and isinstance(downloads[-1], dict) else {}
    return {
        "status": payload.get("status") or "",
        "symbol_count": payload.get("symbol_count") or 0,
        "target_end": payload.get("target_end") or "",
        "days": payload.get("days") or 0,
        "download_count": len(downloads),
        "rate_limit_or_permission_stop": bool(payload.get("rate_limit_or_permission_stop")),
        "last_download": _sanitize(last_download),
    }


def _is_self_process_query(command_line: str) -> bool:
    lowered = command_line.lower()
    return "get-ciminstance win32_process" in lowered and (
        "tushare_minute_probe_and_download.py" in lowered or "tushare_proxy_minutes_250d" in lowered
    )


def _process_matches(job_dir: Path) -> list[dict[str, Any]]:
    """Best-effort Windows process check; empty on unsupported platforms."""

    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -like '*tushare_minute_probe_and_download.py*' "
        f"-or $_.CommandLine -like '*{job_dir.name}*') }} | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(  # noqa: S603,S607 - local read-only process query
            ["powershell", "-NoProfile", "-Command", script],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        parsed = json.loads(completed.stdout)
    except Exception:
        return []
    rows = parsed if isinstance(parsed, list) else [parsed]
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        command_line = _redact_text(str(row.get("CommandLine") or ""))
        if _is_self_process_query(command_line):
            continue
        out.append(
            {
                "pid": row.get("ProcessId"),
                "parent_pid": row.get("ParentProcessId"),
                "name": row.get("Name") or "",
                "command_line": command_line,
            }
        )
    return out


def build_tushare_minute_status(
    *,
    project_root: str | Path | None = None,
    include_processes: bool = True,
    stale_after_seconds: int = 900,
) -> dict[str, Any]:
    root = _root(project_root)
    job_dir = _latest_job(root)
    now = _utc_now()
    if job_dir is None:
        return {
            "task_type": "tushare_minute_status",
            "status": "missing",
            "job": {"path": "", "name": ""},
            "warnings": ["missing:tushare_minute_job"],
            "suggested_next_action": "no_minute_job_found",
            "read_only": True,
            "network_used": False,
            "token_output": False,
        }

    preflight_path = job_dir / "preflight.log"
    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"
    summary_path = job_dir / "summary.json"
    activity_timestamp = _latest_activity_timestamp(
        [preflight_path, stdout_path, stderr_path, summary_path],
        fallback=job_dir.stat().st_mtime,
    )
    modified_at = _iso_from_timestamp(activity_timestamp)
    age_seconds = max(0.0, (now - datetime.fromtimestamp(activity_timestamp, tz=timezone.utc)).total_seconds())

    preflight_tail = _tail_lines(preflight_path, limit=12)
    stdout_tail = _tail_lines(stdout_path, limit=12)
    stderr_tail = _tail_lines(stderr_path, limit=12)
    counters = _count_patterns(stdout_path)
    stderr_counters = _count_patterns(stderr_path)
    for key, value in stderr_counters.items():
        counters[key] = counters.get(key, 0) + value

    summary = _read_summary(summary_path)
    process_rows = _process_matches(job_dir) if include_processes else []
    started_downloader = any(
        "starting python downloader" in line.lower() or "start minute remaining child" in line.lower()
        for line in preflight_tail
    )
    has_stdout = stdout_path.exists() and stdout_path.stat().st_size > 0
    has_summary = bool(summary)
    has_rate_or_permission = (
        bool(summary.get("rate_limit_or_permission_stop"))
        or counters.get("rate_limit_count", 0) > 0
        or counters.get("permission_count", 0) > 0
    )
    has_traceback = counters.get("traceback_count", 0) > 0
    status = str(summary.get("status") or "").strip() if has_summary else ""
    has_recent_running_activity = (
        bool(process_rows)
        or (status == "running" and age_seconds <= stale_after_seconds)
        or (counters.get("symbol_done_count", 0) > 0 and age_seconds <= stale_after_seconds)
    )

    if has_rate_or_permission:
        overall_status = "stopped_rate_or_permission"
        suggested = "wait_for_user_before_restart"
    elif has_traceback and has_recent_running_activity:
        overall_status = "running_with_warnings"
        suggested = "continue_monitoring"
    elif has_traceback:
        overall_status = "error_traceback"
        suggested = "inspect_error_before_restart"
    elif status == "completed" or counters.get("completed_count", 0) > 0:
        overall_status = "completed"
        suggested = "audit_minute_coverage"
    elif process_rows:
        overall_status = "running"
        suggested = "continue_monitoring"
    elif not started_downloader:
        overall_status = "waiting_for_secure_token_input"
        suggested = "paste_token_in_secure_window"
    elif not has_stdout and not has_summary:
        overall_status = "starting"
        suggested = "continue_monitoring"
    elif status == "running" and age_seconds <= stale_after_seconds:
        overall_status = "running"
        suggested = "continue_monitoring"
    elif age_seconds > stale_after_seconds:
        overall_status = "stale_or_stopped"
        suggested = "ask_user_before_restart"
    else:
        overall_status = "recent_activity_no_process_match"
        suggested = "continue_monitoring"

    symbol_done = int(counters.get("symbol_done_count", 0))
    symbol_count = int(summary.get("symbol_count") or 0)
    progress_pct = round(symbol_done * 100.0 / symbol_count, 3) if symbol_count else 0.0

    payload: dict[str, Any] = {
        "task_type": "tushare_minute_status",
        "status": overall_status,
        "job": {
            "path": _relative(job_dir, root),
            "name": job_dir.name,
            "modified_at": modified_at,
            "age_seconds": age_seconds,
        },
        "summary": summary,
        "log_summary": {
            **counters,
            "symbol_count": symbol_count,
            "symbol_done_count": symbol_done,
            "progress_pct": progress_pct,
            "last_symbol_done": _last_symbol_done(stdout_path),
        },
        "logs": {
            "preflight_tail": preflight_tail,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        },
        "processes": {"checked": bool(include_processes), "matches": process_rows},
        "warnings": [],
        "suggested_next_action": suggested,
        "read_only": True,
        "network_used": False,
        "token_output": False,
    }
    if has_traceback:
        payload["warnings"].append(f"traceback_count:{counters.get('traceback_count', 0)}")
    if overall_status == "running_with_warnings":
        payload["warnings"].append("running_job_has_nonfatal_traceback")
    payload["latest_completed_quality"] = summarize_completed_minute_quality_snapshot(
        load_latest_tushare_minute_quality_snapshot(project_root=root)
    )
    text = json.dumps(payload, ensure_ascii=False)
    if re.search(r"\b[a-fA-F0-9]{32,}\b", text):
        payload["warnings"].append("token_like_text_redacted_check_failed")
    return payload


def render_tushare_minute_status_report(payload: dict[str, Any]) -> str:
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    logs = payload.get("log_summary") if isinstance(payload.get("log_summary"), dict) else {}
    last = logs.get("last_symbol_done") if isinstance(logs.get("last_symbol_done"), dict) else {}
    latest_quality = (
        payload.get("latest_completed_quality") if isinstance(payload.get("latest_completed_quality"), dict) else {}
    )
    return "\n".join(
        [
            "# Tushare Minute Status",
            "",
            f"- status: `{payload.get('status')}`",
            f"- job: `{job.get('path') or '-'}`",
            f"- summary_status: `{summary.get('status') or '-'}`",
            f"- symbols: `{summary.get('symbol_count') or logs.get('symbol_count') or 0}`",
            f"- symbol_done: `{logs.get('symbol_done_count', 0)}`",
            f"- progress_pct: `{logs.get('progress_pct', 0.0)}`",
            f"- last_symbol: `{last.get('symbol') or '-'}`",
            f"- rate_limit_count: `{logs.get('rate_limit_count', 0)}`",
            f"- permission_count: `{logs.get('permission_count', 0)}`",
            f"- traceback_count: `{logs.get('traceback_count', 0)}`",
            (
                "- latest_completed_quality: "
                f"`{latest_quality.get('status') or '-'}` "
                f"usable=`{latest_quality.get('usable_count', 0)}/{latest_quality.get('total_symbols', 0)}` "
                f"invalid=`{latest_quality.get('invalid_count', 0)}` "
                f"missing=`{latest_quality.get('missing_count', 0)}` "
                f"target=`{latest_quality.get('target_date') or '-'}` "
                f"action=`{latest_quality.get('suggested_next_action') or '-'}`"
            ),
            f"- suggested_next_action: `{payload.get('suggested_next_action')}`",
            "",
            "Read-only / no token output / no network fetch / no broker.",
        ]
    )


def write_tushare_minute_status_outputs(
    *,
    project_root: str | Path | None = None,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    include_processes: bool = True,
    stale_after_seconds: int = 900,
) -> dict[str, Any]:
    payload = build_tushare_minute_status(
        project_root=project_root,
        include_processes=include_processes,
        stale_after_seconds=stale_after_seconds,
    )
    payload["output_path"] = str(output_path)
    payload["report_path"] = str(report_path)
    write_json(output_path, payload)
    write_text(report_path, render_tushare_minute_status_report(payload))
    return payload
