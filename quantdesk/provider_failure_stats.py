"""Provider failure statistics from existing local smoke results and logs."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quantdesk.operation_log import load_operation_log, sanitize_for_operation_log
from quantdesk.shared import now_text, read_json, short_error_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SMOKE_PATH = PROJECT_ROOT / "results" / "provider_network_smoke_latest.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "results" / "operation_log" / "operation_timeline.jsonl"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "provider_failure_stats_latest.json"
DISCLAIMER = "provider 失败统计仅用于诊断数据源健康，不构成投资建议。"


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target)


def classify_provider_failure_reason(reason: Any, *, status: str = "") -> str:
    text = str(reason or status or "").strip().lower()
    if not text:
        return "unknown"
    if "network=false" in text or "network disabled" in text or "network_disabled" in text or "offline" in text or "disabled" in text:
        return "network_disabled"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "empty" in text or "no data" in text or "no_data" in text:
        return "empty_response"
    if "parse" in text or "json" in text or "decode" in text:
        return "parse_error"
    if "missing" in text or "not installed" in text or "import" in text:
        return "dependency_missing"
    if "skipped" in text:
        return "skipped"
    return "provider_failed"


def load_provider_smoke_results(path: str | Path = DEFAULT_SMOKE_PATH) -> dict[str, Any]:
    payload = read_json(_project_path(path), {})
    return payload if isinstance(payload, dict) else {}


def _within_lookback(row: dict[str, Any], lookback_days: int) -> bool:
    text = str(row.get("finished_at") or row.get("started_at") or "")
    if not text:
        return True
    try:
        when = datetime.fromisoformat(text[:19])
    except Exception:
        return True
    return when >= datetime.now() - timedelta(days=max(0, int(lookback_days or 0)))


def load_operation_log_provider_failures(
    log_path: str | Path = DEFAULT_LOG_PATH,
    *,
    lookback_days: int = 7,
) -> list[dict[str, Any]]:
    rows = load_operation_log(_project_path(log_path))
    provider_commands = {"network-smoke", "providers", "data-health"}
    out: list[dict[str, Any]] = []
    for row in rows:
        command = str(row.get("command") or "").split()[0]
        if command not in provider_commands or not _within_lookback(row, lookback_days):
            continue
        status = str(row.get("status") or "")
        reason = row.get("failed_reason") or "; ".join(str(item) for item in (row.get("warnings") or []))
        if status in {"failed", "warning"} or reason:
            out.append(
                {
                    "provider": command,
                    "status": status,
                    "reason": short_error_text(reason),
                    "type": classify_provider_failure_reason(reason, status=status),
                    "source": "operation_log",
                }
            )
    return out


def _smoke_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    rows: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        status = str(step.get("status") or "").lower()
        reason = step.get("failed_reason") or step.get("skipped_reason") or ""
        failure_type = classify_provider_failure_reason(reason, status=status)
        provider = str(step.get("provider_group") or step.get("provider") or step.get("provider_used") or "unknown")
        rows.append(
            {
                "provider": provider,
                "symbol": step.get("symbol") or "",
                "status": status,
                "reason": short_error_text(reason),
                "type": failure_type,
                "placeholder": bool(step.get("placeholder") or step.get("planned") or status == "planned"),
                "source": "provider_network_smoke",
            }
        )
    return rows


def summarize_provider_failures(rows: list[dict[str, Any]]) -> dict[str, Any]:
    failures_by_provider: Counter[str] = Counter()
    failures_by_type: Counter[str] = Counter()
    skipped_by_type: Counter[str] = Counter()
    failed_symbols: Counter[str] = Counter()
    failed_count = 0
    skipped_count = 0
    for row in rows:
        status = str(row.get("status") or "").lower()
        failure_type = str(row.get("type") or "unknown")
        provider = str(row.get("provider") or "unknown")
        is_placeholder = bool(row.get("placeholder") or row.get("planned") or status in {"planned", "not_implemented"})
        if is_placeholder:
            skipped_count += 1
            skipped_by_type["planned_provider"] += 1
            continue
        is_network_disabled = failure_type == "network_disabled"
        if status == "skipped" or is_network_disabled or failure_type == "skipped":
            skipped_count += 1
            skipped_by_type[failure_type] += 1
            continue
        if status in {"failed", "error"} or row.get("reason"):
            failed_count += 1
            failures_by_provider[provider] += 1
            failures_by_type[failure_type] += 1
            if row.get("symbol"):
                failed_symbols[str(row.get("symbol"))] += 1
    return {
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "offline_skipped_count": skipped_by_type.get("network_disabled", 0),
        "failures_by_provider": dict(failures_by_provider),
        "failures_by_type": dict(failures_by_type),
        "skipped_by_type": dict(skipped_by_type),
        "top_failed_symbols": [symbol for symbol, _count in failed_symbols.most_common(10)],
    }


def collect_provider_failure_stats(
    *,
    smoke_path: str | Path = DEFAULT_SMOKE_PATH,
    log_path: str | Path = DEFAULT_LOG_PATH,
    lookback_days: int = 7,
    network: bool = False,
    write: bool = False,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
) -> dict[str, Any]:
    smoke = load_provider_smoke_results(smoke_path)
    rows = _smoke_rows(smoke)
    log_rows = load_operation_log_provider_failures(log_path, lookback_days=lookback_days)
    rows.extend(log_rows)
    summary = summarize_provider_failures(rows)
    warnings: list[str] = []
    if not smoke and not _project_path(log_path).exists():
        warnings.append("未找到 provider smoke 结果或 operation_log；provider 失败统计为空。")
    if summary["offline_skipped_count"]:
        warnings.append("存在 network_disabled 记录，这代表离线跳过，不算真实 provider 故障。")
    status = "warning" if warnings or summary["failed_count"] else "passed"
    providers = sorted({str(row.get("provider") or "unknown") for row in rows})
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "provider_failure_stats",
        "status": status,
        "lookback_days": int(lookback_days),
        "provider_count": len(providers),
        "network": bool(network),
        "full_scan": False,
        **summary,
        "rows": sanitize_for_operation_log(rows[-100:]),
        "warnings": [short_error_text(item) for item in warnings if item],
        "suggested_next_action": (
            "如果 network_disabled 较多，说明当前多数检查为离线模式；如需联网烟测，请显式 --enable-network。"
            if summary["offline_skipped_count"] or warnings
            else "provider 失败统计暂无明显异常；可继续检查数据新鲜度和分钟线覆盖率。"
        ),
        "generated_at": now_text(),
        "sources": {"smoke_path": _rel(smoke_path), "operation_log": _rel(log_path)},
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_provider_failure_stats(payload, output_path)
    return payload


def write_provider_failure_stats(payload: dict[str, Any], output_path: str | Path = DEFAULT_OUTPUT_PATH) -> Path:
    return write_json(_project_path(output_path), payload)
