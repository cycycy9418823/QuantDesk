"""Read-only retry planning for local Tushare queue failures.

This module is intentionally not a downloader.  It reads local queue status and
logs, groups transient-looking failures, and tells operators when to wait,
review access/rate-limit issues, or explicitly approve a later retry.

It must never print provider tokens, fetch network data, restart processes,
kill processes, import staged data, or write ``data/daily``.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from quantdesk.shared import write_json, write_text
from quantdesk.tushare_queue_status import build_tushare_queue_status


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "tushare_retry_plan_latest.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "tushare_retry_plan_latest.md"

QUEUE_GLOB = "data/vendor_staging/tushare_continuous_queue_*"
FAILURE_LEVEL_RE = re.compile(r"^(?P<level>FAIL|RETRY)\s+(?P<api>[A-Za-z0-9_]+)\s+params=(?P<rest>.*)$")
TOKEN_RE = re.compile(r"\b[a-fA-F0-9]{32,}\b")


@dataclass
class FailureGroup:
    stage: str
    api: str
    reason_class: str
    fail_count: int = 0
    retry_count: int = 0
    symbols: set[str] = field(default_factory=set)
    examples: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self, *, max_examples: int = 5) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "api": self.api,
            "reason_class": self.reason_class,
            "fail_count": self.fail_count,
            "retry_count": self.retry_count,
            "symbols": sorted(self.symbols)[:50],
            "symbol_count": len(self.symbols),
            "examples": self.examples[:max_examples],
        }


def _root(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _redact_text(text: str) -> str:
    redacted = str(text)
    redacted = re.sub(r"([?&]token=)[^&\s]+", r"\1<redacted-token>", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"\b(token|api[_-]?key|apikey)\s*[:=]\s*[^,\s]+", r"\1=<redacted-token>", redacted, flags=re.IGNORECASE)
    redacted = TOKEN_RE.sub("<redacted-token-like>", redacted)
    return redacted


def _latest_queue(root: Path) -> Path | None:
    queues = [path for path in root.glob(QUEUE_GLOB) if path.is_dir()]
    if not queues:
        return None
    return sorted(queues, key=lambda item: (item.name, item.stat().st_mtime), reverse=True)[0]


def _iter_log_files(queue: Path) -> list[Path]:
    candidates: list[Path] = []
    for base in (queue / "logs", queue):
        if base.exists():
            candidates.extend(path for path in base.glob("*.log") if path.is_file())
    seen: set[Path] = set()
    out: list[Path] = []
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(path)
    return out


def _stage_from_log_name(path: Path) -> str:
    name = path.name
    for suffix in ("_stderr.log", "_stdout.log", ".log"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _reason_class(reason: str) -> str:
    text = str(reason or "").lower()
    if "permission" in text or "forbidden" in text or "denied" in text or "权限" in text or "积分" in text:
        return "permission"
    if "429" in text or "rate" in text and "limit" in text or "requests-per-minute" in text or "rpm" in text:
        return "rate_limit"
    if "unexpected_eof" in text or "ssl" in text and "eof" in text:
        return "ssl_eof"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "traceback" in text or "exception" in text or "fatal error" in text:
        return "traceback"
    if "network" in text or "urlopen" in text:
        return "network"
    return "other"


def _find_param_end(text: str) -> int:
    if not text.startswith("{"):
        return -1
    depth = 0
    in_string: str | None = None
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == in_string:
                in_string = None
            continue
        if char in {"'", '"'}:
            in_string = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _parse_params(raw: str) -> dict[str, Any]:
    try:
        value = ast.literal_eval(raw)
    except Exception:
        return {}
    if isinstance(value, dict):
        return {str(key): _redact_text(str(item)) for key, item in value.items()}
    return {}


def _split_failure_rest(rest: str) -> tuple[str, str]:
    if " error=" in rest:
        return rest.split(" error=", 1)
    if " reason=" in rest:
        return rest.split(" reason=", 1)
    return rest, ""


def parse_failure_log_line(line: str, *, stage: str, path: Path, root: Path) -> dict[str, Any] | None:
    """Parse one local queue log line into a sanitized failure/retry event."""

    text = _redact_text(str(line).strip())
    match = FAILURE_LEVEL_RE.match(text)
    if not match:
        return None
    before_reason, reason = _split_failure_rest(match.group("rest"))
    param_text = before_reason.strip()
    param_end = _find_param_end(param_text)
    if param_end >= 0:
        params_raw = param_text[: param_end + 1]
        detail_text = param_text[param_end + 1 :].strip()
    else:
        params_raw = param_text
        detail_text = ""
    params = _parse_params(params_raw)
    level = match.group("level").upper()
    symbol = str(params.get("ts_code") or params.get("symbol") or params.get("code") or "").strip()
    reason_text = _redact_text(reason.strip() or detail_text)
    return {
        "level": level,
        "stage": stage,
        "api": match.group("api"),
        "reason": reason_text,
        "reason_class": _reason_class(reason_text),
        "params": params,
        "symbol": symbol,
        "log_path": _relative(path, root),
        "line": text,
    }


def _collect_failure_events(queue: Path, root: Path, *, max_events: int = 10000) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in _iter_log_files(queue):
        stage = _stage_from_log_name(path)
        try:
            with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                for line in handle:
                    event = parse_failure_log_line(line, stage=stage, path=path, root=root)
                    if event is None:
                        continue
                    events.append(event)
                    if len(events) >= max_events:
                        return events
        except Exception:
            continue
    return events


def _group_events(events: Iterable[dict[str, Any]], *, max_examples_per_group: int = 5) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], FailureGroup] = {}
    for event in events:
        key = (str(event.get("stage") or ""), str(event.get("api") or ""), str(event.get("reason_class") or "other"))
        group = groups.setdefault(key, FailureGroup(stage=key[0], api=key[1], reason_class=key[2]))
        if event.get("level") == "FAIL":
            group.fail_count += 1
        elif event.get("level") == "RETRY":
            group.retry_count += 1
        symbol = str(event.get("symbol") or "").strip()
        if symbol:
            group.symbols.add(symbol)
        if len(group.examples) < max_examples_per_group:
            group.examples.append(
                {
                    "level": event.get("level"),
                    "symbol": symbol,
                    "params": event.get("params") or {},
                    "reason": event.get("reason"),
                    "log_path": event.get("log_path"),
                }
            )
    return [
        group.to_payload(max_examples=max_examples_per_group)
        for group in sorted(groups.values(), key=lambda item: (item.fail_count, item.retry_count, item.stage, item.api), reverse=True)
    ]


def _retry_candidates(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retryable_classes = {"ssl_eof", "timeout", "network", "other"}
    candidates: list[dict[str, Any]] = []
    for group in groups:
        if int(group.get("fail_count") or 0) <= 0:
            continue
        if str(group.get("reason_class") or "") not in retryable_classes:
            continue
        candidates.append(
            {
                "stage": group.get("stage"),
                "api": group.get("api"),
                "reason_class": group.get("reason_class"),
                "fail_count": group.get("fail_count"),
                "symbol_count": group.get("symbol_count"),
                "symbols": group.get("symbols") or [],
            }
        )
    return candidates


def _summary(events: list[dict[str, Any]], groups: list[dict[str, Any]]) -> dict[str, Any]:
    fail_count = sum(1 for event in events if event.get("level") == "FAIL")
    retry_count = sum(1 for event in events if event.get("level") == "RETRY")
    by_reason: dict[str, int] = {}
    for event in events:
        reason = str(event.get("reason_class") or "other")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "event_count": len(events),
        "fail_event_count": fail_count,
        "retry_event_count": retry_count,
        "group_count": len(groups),
        "ssl_eof_count": by_reason.get("ssl_eof", 0),
        "timeout_count": by_reason.get("timeout", 0),
        "rate_limit_count": by_reason.get("rate_limit", 0),
        "permission_count": by_reason.get("permission", 0),
        "traceback_count": by_reason.get("traceback", 0),
        "network_count": by_reason.get("network", 0),
        "other_count": by_reason.get("other", 0),
        "by_reason_class": by_reason,
    }


def _choose_gate(queue_status: str, summary: dict[str, Any]) -> tuple[str, str, str]:
    status = str(queue_status or "").strip().lower()
    has_failures = int(summary.get("fail_event_count") or 0) > 0
    has_retries = int(summary.get("retry_event_count") or 0) > 0
    if status in {"running", "starting", "in_progress", "unknown"}:
        return "warning", "wait_for_queue_completion", "continue_monitoring"
    if not has_failures and not has_retries:
        return "passed", "no_retry_needed", "run_audit_extended_import"
    if int(summary.get("traceback_count") or 0) > 0:
        return "warning", "inspect_traceback_before_retry", "inspect_logs_before_restart"
    if int(summary.get("permission_count") or 0) > 0 or int(summary.get("rate_limit_count") or 0) > 0:
        return "warning", "review_failures_before_retry", "review_access_or_rate_limit_then_retry_with_user_approval"
    if has_failures:
        return "warning", "review_failures_before_retry", "review_retry_plan_then_restart_with_user_approval"
    return "warning", "review_retries", "review_retry_plan"


def build_tushare_retry_plan(
    *,
    project_root: str | Path | None = None,
    max_events: int = 10000,
    max_examples_per_group: int = 5,
) -> dict[str, Any]:
    """Build a read-only retry plan from local Tushare queue logs."""

    root = _root(project_root)
    now = _utc_now()
    queue = _latest_queue(root)
    if queue is None:
        return {
            "task_type": "tushare_retry_plan",
            "status": "missing",
            "generated_at": now.isoformat(),
            "queue": {"path": "", "status": "missing", "current_stage": ""},
            "summary": {
                "event_count": 0,
                "fail_event_count": 0,
                "retry_event_count": 0,
                "group_count": 0,
            },
            "failure_groups": [],
            "retry_candidates": [],
            "retry_gate": "no_queue_found",
            "safe_to_auto_restart": False,
            "requires_user_confirmation": True,
            "suggested_next_action": "no_queue_found",
            "warnings": ["missing:tushare_queue"],
            "research_only": True,
            "no_network_fetch": True,
            "no_broker": True,
        }
    queue_status_payload = build_tushare_queue_status(project_root=root, include_processes=False)
    queue_info = queue_status_payload.get("queue") if isinstance(queue_status_payload.get("queue"), dict) else {}
    queue_status = str(queue_info.get("status") or queue_status_payload.get("status") or "unknown")
    events = _collect_failure_events(queue, root, max_events=max_events)
    groups = _group_events(events, max_examples_per_group=max_examples_per_group)
    summary = _summary(events, groups)
    status, gate, suggested = _choose_gate(queue_status, summary)
    warnings: list[str] = []
    if gate == "wait_for_queue_completion":
        warnings.append("download_queue_still_running")
    if int(summary.get("fail_event_count") or 0) > 0:
        warnings.append(f"fail_events:{summary['fail_event_count']}")
    if int(summary.get("permission_count") or 0) > 0:
        warnings.append(f"permission_events:{summary['permission_count']}")
    if int(summary.get("rate_limit_count") or 0) > 0:
        warnings.append(f"rate_limit_events:{summary['rate_limit_count']}")
    if len(events) >= max_events:
        warnings.append(f"event_limit_reached:{max_events}")
    retry_candidates = _retry_candidates(groups)
    return {
        "task_type": "tushare_retry_plan",
        "status": status,
        "generated_at": now.isoformat(),
        "queue": {
            "path": _relative(queue, root),
            "status": queue_status,
            "current_stage": queue_info.get("current_stage") or "",
            "current_stage_status": queue_info.get("current_stage_status") or "",
        },
        "summary": summary,
        "failure_groups": groups,
        "retry_candidates": retry_candidates,
        "retry_gate": gate,
        "safe_to_auto_restart": False,
        "requires_user_confirmation": bool(events) or gate != "no_retry_needed",
        "suggested_next_action": suggested,
        "warnings": warnings,
        "operator_notes": [
            "read_only_retry_plan",
            "do_not_restart_without_user_confirmation",
            "do_not_import_while_queue_running",
            "tokens_redacted",
        ],
        "research_only": True,
        "no_network_fetch": True,
        "no_broker": True,
    }


def render_tushare_retry_plan_report(payload: dict[str, Any]) -> str:
    queue = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# Tushare Retry Plan",
        "",
        "> Research Only / Read-Only Local Log Review / No Network Fetch / No Auto Restart / No Broker Route",
        "",
        f"- Status: `{payload.get('status') or '-'}`",
        f"- Retry gate: `{payload.get('retry_gate') or '-'}`",
        f"- Suggested next action: `{payload.get('suggested_next_action') or '-'}`",
        f"- Queue: `{queue.get('path') or '-'}`",
        f"- Queue status: `{queue.get('status') or '-'}`",
        f"- Current stage: `{queue.get('current_stage') or '-'}`",
        f"- Safe to auto restart: `{payload.get('safe_to_auto_restart')}`",
        "",
        "## Summary",
        "",
        f"- events: {summary.get('event_count', 0)}",
        f"- FAIL events: {summary.get('fail_event_count', 0)}",
        f"- RETRY events: {summary.get('retry_event_count', 0)}",
        f"- groups: {summary.get('group_count', 0)}",
        f"- SSL EOF: {summary.get('ssl_eof_count', 0)}",
        f"- timeout: {summary.get('timeout_count', 0)}",
        f"- permission: {summary.get('permission_count', 0)}",
        f"- rate-limit: {summary.get('rate_limit_count', 0)}",
        "",
        "## Retry candidates",
        "",
    ]
    candidates = payload.get("retry_candidates") if isinstance(payload.get("retry_candidates"), list) else []
    if candidates:
        for item in candidates[:50]:
            lines.append(
                "- "
                f"`{item.get('stage')}` / `{item.get('api')}` / `{item.get('reason_class')}`: "
                f"fail={item.get('fail_count', 0)}, symbols={item.get('symbol_count', 0)}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Failure groups", ""])
    groups = payload.get("failure_groups") if isinstance(payload.get("failure_groups"), list) else []
    if groups:
        for group in groups[:80]:
            lines.append(
                "- "
                f"`{group.get('stage')}` / `{group.get('api')}` / `{group.get('reason_class')}`: "
                f"FAIL={group.get('fail_count', 0)}, RETRY={group.get('retry_count', 0)}, "
                f"symbols={group.get('symbol_count', 0)}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    if warnings:
        lines.extend(f"- `{warning}`" for warning in warnings)
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def write_tushare_retry_plan_outputs(
    *,
    project_root: str | Path | None = None,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    max_events: int = 10000,
    max_examples_per_group: int = 5,
) -> dict[str, Any]:
    payload = build_tushare_retry_plan(
        project_root=project_root,
        max_events=max_events,
        max_examples_per_group=max_examples_per_group,
    )
    output = Path(output_path)
    report = Path(report_path)
    payload["output_path"] = output.as_posix()
    payload["report_path"] = report.as_posix()
    write_json(output, payload)
    write_text(report, render_tushare_retry_plan_report(payload))
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))
