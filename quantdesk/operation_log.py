"""Operation timeline logging for QuantDesk CLI workflows.

The log is deliberately small and local. It records command outcomes and output
paths for troubleshooting, while filtering secrets and avoiding any provider or
LLM calls.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from quantdesk.shared import now_text, read_json, short_error_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_ROOT / "results" / "operation_log"
DEFAULT_TIMELINE_PATH = DEFAULT_LOG_DIR / "operation_timeline.jsonl"
DEFAULT_STATUS_PATH = DEFAULT_LOG_DIR / "latest_operation_status.json"
DISCLAIMER = "仅用于本地操作排查和结果定位，不构成投资建议。"

SECRET_HINTS = ("key", "secret", "token", "password", "passwd", "authorization", "bearer")


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _environment_log_base() -> Path | None:
    """Return the operation-log override dir when the env var is set.

    Tests isolate the real ``results/operation_log`` sink by exporting
    ``QUANTDESK_OPERATION_LOG_DIR`` to a temp directory.  The override only
    applies when callers use the default sink paths so explicit per-test paths
    keep their exact destination.
    """
    raw = os.environ.get("QUANTDESK_OPERATION_LOG_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _resolve_timeline_path(log_path: str | Path) -> str | Path:
    if log_path == DEFAULT_TIMELINE_PATH:
        base = _environment_log_base()
        if base is not None:
            return base / "operation_timeline.jsonl"
    return log_path


def _resolve_status_path(output_path: str | Path) -> str | Path:
    if output_path == DEFAULT_STATUS_PATH:
        base = _environment_log_base()
        if base is not None:
            return base / "latest_operation_status.json"
    return output_path


def _environment_log_base() -> Path | None:
    """Resolve the optional QUANTDESK_OPERATION_LOG_DIR test-isolation override.

    Tests that spawn ``scripts/qd.py`` subprocesses (or call the default-path
    writers directly) set this variable so the real ``results/operation_log``
    timeline is never appended to from a test run.  Production runs never set
    it, so default behavior is unchanged.
    """
    raw = os.environ.get("QUANTDESK_OPERATION_LOG_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _resolve_timeline_path(log_path: str | Path) -> str | Path:
    """Redirect the default timeline sink when an env override is active.

    The override applies only when the caller uses the default sink path so
    explicit per-test paths keep their exact destination.
    """
    if log_path == DEFAULT_TIMELINE_PATH:
        base = _environment_log_base()
        if base is not None:
            return base / "operation_timeline.jsonl"
    return log_path


def _resolve_status_path(output_path: str | Path) -> str | Path:
    if output_path == DEFAULT_STATUS_PATH:
        base = _environment_log_base()
        if base is not None:
            return base / "latest_operation_status.json"
    return output_path


def _mask_text(value: str) -> str:
    lowered = value.lower()
    if any(hint in lowered for hint in SECRET_HINTS):
        if "=" in value:
            return value.split("=", 1)[0] + "=***"
        if ":" in value:
            return value.split(":", 1)[0] + ":***"
        return "***"
    if value.startswith(("sk-", "ak-", "pk-")) and len(value) > 10:
        return value[:4] + "***"
    return value


def sanitize_for_operation_log(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            out[key_text] = "***" if any(hint in key_text.lower() for hint in SECRET_HINTS) else sanitize_for_operation_log(item)
        return out
    if isinstance(value, (list, tuple)):
        out: list[Any] = []
        mask_next = False
        for item in value:
            text = str(item)
            if mask_next:
                out.append("***")
                mask_next = False
                continue
            out.append(_mask_text(text) if isinstance(item, str) else sanitize_for_operation_log(item))
            if text.startswith("--") and any(hint in text.lower() for hint in SECRET_HINTS):
                mask_next = True
        return out
    if isinstance(value, str):
        return _mask_text(value)
    return value


def build_operation_record(
    *,
    command: str,
    args: Any = None,
    target_date: str = "",
    symbols: Any = None,
    network: bool | None = None,
    llm_enabled: bool | None = None,
    status: str = "unknown",
    started_at: str = "",
    finished_at: str = "",
    duration_ms: int | None = None,
    output_paths: Any = None,
    warnings: Any = None,
    failed_reason: str = "",
    suggested_next_action: str = "",
) -> dict[str, Any]:
    clean_args = sanitize_for_operation_log(args or [])
    clean_paths = sanitize_for_operation_log(output_paths or [])
    clean_warnings = sanitize_for_operation_log(warnings or [])
    return {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "command": str(command or ""),
        "args": clean_args,
        "target_date": str(target_date or ""),
        "symbols": sanitize_for_operation_log(symbols or []),
        "network": network,
        "llm_enabled": llm_enabled,
        "status": str(status or "unknown"),
        "started_at": started_at or "",
        "finished_at": finished_at or now_text(),
        "duration_ms": int(duration_ms or 0),
        "output_paths": clean_paths,
        "warnings": clean_warnings,
        "failed_reason": short_error_text(failed_reason),
        "suggested_next_action": short_error_text(suggested_next_action),
        "disclaimer": DISCLAIMER,
    }


def append_operation_log(record: dict[str, Any], log_path: str | Path = DEFAULT_TIMELINE_PATH) -> Path:
    target = _project_path(_resolve_timeline_path(log_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = sanitize_for_operation_log(record)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(clean, ensure_ascii=False, default=str) + "\n")
    return target


def load_operation_log(log_path: str | Path = DEFAULT_TIMELINE_PATH, limit: int | None = None) -> list[dict[str, Any]]:
    target = _project_path(_resolve_timeline_path(log_path))
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows[-limit:] if limit else rows


def summarize_recent_operations(records: list[dict[str, Any]], recent: int = 20) -> dict[str, Any]:
    rows = list(records)[-recent:]
    failed = [row for row in rows if str(row.get("status")) == "failed"]
    warning_rows = [
        row
        for row in rows
        if str(row.get("status")) == "warning" or bool(row.get("warnings"))
    ]
    return {
        "recent_count": len(rows),
        "failed_count": len(failed),
        "warning_count": len(warning_rows),
        "latest_command": rows[-1].get("command") if rows else "",
        "latest_status": rows[-1].get("status") if rows else "missing",
        "failed_commands": [row.get("command") for row in failed[-5:]],
        "warning_commands": [row.get("command") for row in warning_rows[-5:]],
        "suggested_next_action": (
            failed[-1].get("suggested_next_action")
            or failed[-1].get("failed_reason")
            if failed
            else "运行 python scripts/qd.py help 查看推荐入口。"
        ),
        "disclaimer": DISCLAIMER,
    }


def write_operation_status(record: dict[str, Any], output_path: str | Path = DEFAULT_STATUS_PATH) -> Path:
    return write_json(_project_path(_resolve_status_path(output_path)), sanitize_for_operation_log(record))


def read_latest_operation_status(output_path: str | Path = DEFAULT_STATUS_PATH) -> dict[str, Any]:
    payload = read_json(_project_path(_resolve_status_path(output_path)), default={})
    return payload if isinstance(payload, dict) else {}


def log_operation(
    *,
    command: str,
    args: Any = None,
    status: str = "unknown",
    started_at: str = "",
    start_time: float | None = None,
    target_date: str = "",
    symbols: Any = None,
    network: bool | None = None,
    llm_enabled: bool | None = None,
    output_paths: Any = None,
    warnings: Any = None,
    failed_reason: str = "",
    suggested_next_action: str = "",
    log_path: str | Path = DEFAULT_TIMELINE_PATH,
    status_path: str | Path = DEFAULT_STATUS_PATH,
) -> dict[str, Any]:
    finished_at = now_text()
    duration_ms = int((time.perf_counter() - start_time) * 1000) if start_time is not None else 0
    record = build_operation_record(
        command=command,
        args=args,
        target_date=target_date,
        symbols=symbols,
        network=network,
        llm_enabled=llm_enabled,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        output_paths=output_paths,
        warnings=warnings,
        failed_reason=failed_reason,
        suggested_next_action=suggested_next_action,
    )
    if os.environ.get("QUANTDESK_WRAPPED_BY_QD") == "1":
        return record
    append_operation_log(record, _resolve_timeline_path(log_path))
    write_operation_status(record, _resolve_status_path(status_path))
    return record

