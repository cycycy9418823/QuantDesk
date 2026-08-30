"""Validate existing QuantDesk result JSON payloads.

The validator is read-only. It scans selected runtime result files for the
common schema contract, status values, warnings shape, disclaimers, and obvious
secret leakage. Missing files are reported as `missing` instead of process
failures so clean-zip environments remain usable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from quantdesk.schema_contracts import (
    ALLOWED_STATUSES,
    DEFAULT_DISCLAIMER,
    STATUS_ALIASES,
    build_base_result,
    normalize_status,
)
from quantdesk.shared import now_text, short_error_text, write_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "results"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_OUTPUT_PATH = DEFAULT_RESULTS_DIR / "schema_validation_latest.json"
DEFAULT_REPORT_PATH = DEFAULT_REPORTS_DIR / "schema_validation_report_latest.md"
SECRET_KEYWORDS = (
    "api_key",
    "secret",
    "token",
    "access_token",
    "authorization",
    "bearer",
    "password",
    "provider_secrets",
    "llm_config",
    "quantdesk_llm_api_key",
)
TRACEBACK_MARKERS = ("traceback (most recent call last)", "stack trace", "exception:")
LOW_RISK_REFERENCE_MARKERS = (
    "provider_secrets.json",
    "llm_config.json",
    "example",
    "demo",
    "placeholder",
    "your_api_key",
    "your-token",
    "masked",
    "不写入 api key",
    "不要写入 api key",
)


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


def _mask_secret(value: Any) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "****"
    return f"{text[:4]}****{text[-4:]}"


def _looks_secret_text(value: Any) -> bool:
    text = str(value or "").lower()
    return any(marker.lower() in text for marker in SECRET_KEYWORDS)


def _last_path_part(path: str) -> str:
    text = str(path or "")
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return re.sub(r"\[\d+\]$", "", text).lower()


def _is_masked_value(value: str) -> bool:
    text = str(value or "").strip()
    return bool(re.search(r"[*]{3,}", text)) or bool(re.search(r".{2,}\*{2,}.{2,}", text))


def _is_placeholder_value(value: str) -> bool:
    text = str(value or "").strip().lower()
    if text in {"", "none", "null", "false", "true", "0", "-", "n/a", "na"}:
        return True
    if _is_masked_value(text):
        return True
    return any(marker in text for marker in LOW_RISK_REFERENCE_MARKERS)


def _looks_randomish_secret(value: str) -> bool:
    text = str(value or "").strip()
    if len(text) < 12:
        return False
    if text.startswith(("sk-", "sk_", "xoxb-", "ghp_", "AKIA")):
        return True
    has_alpha = bool(re.search(r"[A-Za-z]", text))
    has_digit = bool(re.search(r"\d", text))
    has_symbol = bool(re.search(r"[_\-\.=:/+]", text))
    return has_alpha and (has_digit or has_symbol)


def _classify_secret_finding(path: str, value: Any) -> dict[str, str] | None:
    value_text = str(value or "")
    path_text = str(path or "")
    lower_path = path_text.lower()
    lower_value = value_text.lower()
    key_hit = _looks_secret_text(path_text)
    value_hit = isinstance(value, str) and _looks_secret_text(value_text)
    masked_only = isinstance(value, str) and _is_masked_value(value_text)
    if not key_hit and not value_hit and not masked_only:
        return None
    field_name = _last_path_part(path_text)
    reference_hit = any(marker in lower_path or marker in lower_value for marker in LOW_RISK_REFERENCE_MARKERS)
    if masked_only or reference_hit or _is_placeholder_value(value_text):
        kind = "secret_like_reference"
        severity = "low"
        reason = "敏感词引用、示例、路径名、空值或 masked value；不是确认泄露。"
    elif field_name in {"api_key", "quantdesk_llm_api_key"} and _looks_randomish_secret(value_text):
        kind = "confirmed_secret"
        severity = "high"
        reason = "api_key 字段包含疑似真实密钥值。"
    elif field_name == "authorization" and "bearer " in lower_value and _looks_randomish_secret(value_text):
        kind = "confirmed_secret"
        severity = "high"
        reason = "authorization 字段包含 Bearer token。"
    elif field_name in {"access_token", "token"} and _looks_randomish_secret(value_text):
        kind = "confirmed_secret"
        severity = "high"
        reason = "token 字段包含较长随机样式值。"
    elif field_name in {"password", "secret"} and len(value_text.strip()) >= 8:
        kind = "confirmed_secret"
        severity = "medium"
        reason = "敏感字段包含非空真实样式值。"
    elif value_hit:
        kind = "secret_like_reference"
        severity = "low"
        reason = "文本中出现敏感词引用；未发现真实密钥值。"
    else:
        kind = "secret_like_reference"
        severity = "low"
        reason = "字段名含敏感词但值为空、占位或不可确认。"
    return {
        "severity": severity,
        "kind": kind,
        "field_path": short_error_text(path_text, max_len=180),
        "masked_preview": _mask_secret(value_text) if value_text else "",
        "reason": reason,
        "suggested_next_action": (
            "立即移除真实密钥并轮换凭据。"
            if kind == "confirmed_secret"
            else "若高风险密钥泄露为 0，通常无需处理；可查看 schema-check 报告。"
        ),
    }


def _walk(value: Any, path: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            rows.extend(_walk(item, next_path))
        return rows
    if isinstance(value, list):
        for index, item in enumerate(value):
            rows.extend(_walk(item, f"{path}[{index}]"))
        return rows
    rows.append((path, value))
    return rows


def validate_no_secret_leak(payload: Any) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    for path, value in _walk(payload):
        finding = _classify_secret_finding(path, value)
        if finding:
            findings.append(finding)
    confirmed = [row for row in findings if row.get("kind") == "confirmed_secret"]
    references = [row for row in findings if row.get("kind") == "secret_like_reference"]
    return {
        "ok": not confirmed,
        "secret_leak_count": len(confirmed),
        "confirmed_secret_leak_count": len(confirmed),
        "secret_like_reference_count": len(references),
        "secret_findings": findings,
        "secret_leaks": confirmed,
    }


def _detect_traceback(payload: Any) -> list[str]:
    hits: list[str] = []
    for path, value in _walk(payload):
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        if any(marker in lowered for marker in TRACEBACK_MARKERS):
            hits.append(short_error_text(path, max_len=180))
    return hits


def validate_required_common_fields(payload: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for field in ("schema_version", "status", "warnings", "disclaimer"):
        if field not in payload:
            warnings.append(f"missing_{field}")
    if "app_version" not in payload and "package_version" not in payload:
        warnings.append("missing_app_or_package_version")
    return warnings


def validate_disclaimer_present(payload: dict[str, Any]) -> bool:
    return bool(str(payload.get("disclaimer") or "").strip())


def validate_status_values(payload: dict[str, Any]) -> dict[str, Any]:
    raw = str(payload.get("status") or "").strip().lower()
    if not raw:
        return {"ok": False, "status": "missing", "normalized_status": "missing", "reason": "missing_status"}
    if raw in ALLOWED_STATUSES or raw in STATUS_ALIASES:
        return {"ok": True, "status": raw, "normalized_status": normalize_status(raw), "reason": ""}
    return {"ok": False, "status": raw, "normalized_status": "failed", "reason": "illegal_status"}


def validate_schema_version(payload: dict[str, Any]) -> dict[str, Any]:
    version = payload.get("schema_version")
    return {
        "ok": bool(version),
        "schema_version": str(version or ""),
        "reason": "" if version else "missing_schema_version",
    }


def validate_result_payload(payload: Any, *, source: str = "", strict: bool = False) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    if not isinstance(payload, dict):
        return {
            "source": source,
            "status": "failed",
            "ok": False,
            "warnings": [],
            "errors": ["payload_not_dict"],
            "secret_leak_count": 0,
            "confirmed_secret_leak_count": 0,
            "secret_like_reference_count": 0,
            "secret_findings": [],
        }
    warnings.extend(validate_required_common_fields(payload))
    schema = validate_schema_version(payload)
    if not schema.get("ok"):
        warnings.append("missing_schema_version")
    status_check = validate_status_values(payload)
    if not status_check.get("ok"):
        message = str(status_check.get("reason") or "invalid_status")
        if strict and message == "illegal_status":
            errors.append(message)
        else:
            warnings.append(message)
    if "warnings" in payload and not isinstance(payload.get("warnings"), list):
        warnings.append("warnings_not_list")
    if not validate_disclaimer_present(payload):
        warnings.append("missing_disclaimer")
    traceback_hits = _detect_traceback(payload)
    if traceback_hits:
        warnings.append("traceback_or_raw_exception_text_detected")
    secrets = validate_no_secret_leak(payload)
    confirmed_secret_count = int(secrets.get("confirmed_secret_leak_count") or 0)
    reference_count = int(secrets.get("secret_like_reference_count") or 0)
    if confirmed_secret_count:
        if strict:
            errors.append("secret_leak_detected")
        else:
            warnings.append("secret_leak_detected")
    elif reference_count:
        warnings.append("secret_like_reference_detected")
    status = "failed" if errors else ("warning" if warnings else "passed")
    return {
        "source": source,
        "status": status,
        "ok": not errors,
        "schema_version": schema.get("schema_version") or "",
        "raw_status": payload.get("status"),
        "normalized_status": status_check.get("normalized_status"),
        "warnings": sorted(dict.fromkeys(warnings)),
        "errors": sorted(dict.fromkeys(errors)),
        "secret_leak_count": int(secrets.get("secret_leak_count") or 0),
        "confirmed_secret_leak_count": confirmed_secret_count,
        "secret_like_reference_count": reference_count,
        "secret_findings": secrets.get("secret_findings") or [],
        "secret_leaks": secrets.get("secret_leaks") or [],
        "traceback_paths": traceback_hits,
    }


def validate_json_file_payload(path: str | Path, *, strict: bool = False) -> dict[str, Any]:
    target = _project_path(path)
    rel = _rel(target)
    if not target.exists():
        return {
            "source": rel,
            "status": "missing",
            "ok": True,
            "warnings": ["missing_file"],
            "errors": [],
            "secret_leak_count": 0,
            "confirmed_secret_leak_count": 0,
            "secret_like_reference_count": 0,
            "secret_findings": [],
        }
    if not target.is_file():
        return {
            "source": rel,
            "status": "failed" if strict else "warning",
            "ok": not strict,
            "warnings": ["not_a_file"],
            "errors": ["not_a_file"] if strict else [],
            "secret_leak_count": 0,
            "confirmed_secret_leak_count": 0,
            "secret_like_reference_count": 0,
            "secret_findings": [],
        }
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "source": rel,
            "status": "failed",
            "ok": False,
            "warnings": [],
            "errors": [f"json_parse_error: {short_error_text(exc)}"],
            "secret_leak_count": 0,
            "confirmed_secret_leak_count": 0,
            "secret_like_reference_count": 0,
            "secret_findings": [],
        }
    return validate_result_payload(payload, source=rel, strict=strict)


def _default_targets(results_dir: Path) -> list[Path]:
    targets = [
        results_dir / "data_health" / "data_health_dashboard_latest.json",
        results_dir / "strategy_validation_latest.json",
        results_dir / "paper_vs_backtest_consistency_latest.json",
        results_dir / "feature_quality_audit_latest.json",
        results_dir / "backtest_statistical_validation_latest.json",
        results_dir / "parameter_sensitivity_latest.json",
        results_dir / "strategy_observation_context_latest.json",
        results_dir / "strategy_observation_report_audit_latest.json",
        results_dir / "daily_workflow_latest.json",
        results_dir / "market_intelligence" / "market_intelligence_latest.json",
        results_dir / "market_intelligence" / "market_regime_latest.json",
        results_dir / "market_intelligence" / "sector_theme_latest.json",
        results_dir / "market_intelligence" / "market_news_latest.json",
        results_dir / "market_intelligence" / "sector_low_price_candidates_latest.json",
        results_dir / "market_intelligence" / "scenario_curves_latest.json",
        results_dir / "status_latest.json",
        results_dir / "command_center_status_latest.json",
    ]
    retention_root = results_dir / "daily_retention"
    if retention_root.exists():
        targets.extend(sorted(retention_root.glob("*/daily_retention_manifest.json")))
    else:
        targets.append(retention_root / "*" / "daily_retention_manifest.json")
    return targets


def validate_results_directory(
    *,
    results_dir: str | Path = DEFAULT_RESULTS_DIR,
    reports_dir: str | Path = DEFAULT_REPORTS_DIR,
    strict: bool = False,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    write: bool = False,
) -> dict[str, Any]:
    results = _project_path(results_dir)
    reports = _project_path(reports_dir)
    targets = _default_targets(results)
    file_results = [validate_json_file_payload(path, strict=strict) for path in targets]
    passed_count = sum(1 for row in file_results if row.get("status") == "passed")
    warning_count = sum(1 for row in file_results if row.get("status") == "warning")
    failed_count = sum(1 for row in file_results if row.get("status") == "failed")
    missing_count = sum(1 for row in file_results if row.get("status") == "missing")
    secret_leak_count = sum(int(row.get("secret_leak_count") or 0) for row in file_results)
    confirmed_secret_leak_count = sum(int(row.get("confirmed_secret_leak_count") or 0) for row in file_results)
    secret_like_reference_count = sum(int(row.get("secret_like_reference_count") or 0) for row in file_results)
    if failed_count:
        status = "failed"
    elif warning_count or missing_count or secret_leak_count:
        status = "warning"
    else:
        status = "passed"
    payload = build_base_result(
        status=status,
        ok=status != "failed",
        warnings=[],
        generated_at=now_text(),
        suggested_next_action=(
            "检查 schema_validation_report_latest.md 中的 warning；missing 文件在 clean zip 或尚未运行对应命令时是正常现象。"
            if status != "passed"
            else "当前已检查的结果 payload 符合基础 contract。"
        ),
        task_type="schema_validation",
        results_dir=_rel(results),
        reports_dir=_rel(reports),
        strict=bool(strict),
        network=False,
        checked_files=len(file_results),
        passed_count=passed_count,
        warning_count=warning_count,
        failed_count=failed_count,
        missing_count=missing_count,
        secret_leak_count=secret_leak_count,
        confirmed_secret_leak_count=confirmed_secret_leak_count,
        secret_like_reference_count=secret_like_reference_count,
        secret_findings=[
            finding
            for row in file_results
            for finding in (row.get("secret_findings") or [])
            if isinstance(finding, dict)
        ],
        files=file_results,
    )
    if write:
        write_json(output_path, payload)
        target_report = _project_path(report_path)
        target_report.parent.mkdir(parents=True, exist_ok=True)
        target_report.write_text(build_payload_validation_report(payload), encoding="utf-8")
        payload["output_path"] = _rel(output_path)
        payload["report_path"] = _rel(target_report)
    return payload


def build_payload_validation_report(payload: dict[str, Any]) -> str:
    files = payload.get("files") if isinstance(payload.get("files"), list) else []
    lines = [
        "# QuantDesk Schema Validation Report",
        "",
        payload.get("disclaimer") or DEFAULT_DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- checked_files: `{payload.get('checked_files')}`",
        f"- passed_count: `{payload.get('passed_count')}`",
        f"- warning_count: `{payload.get('warning_count')}`",
        f"- failed_count: `{payload.get('failed_count')}`",
        f"- missing_count: `{payload.get('missing_count')}`",
        f"- secret_leak_count: `{payload.get('secret_leak_count')}`",
        f"- confirmed_secret_leak_count: `{payload.get('confirmed_secret_leak_count')}`",
        f"- secret_like_reference_count: `{payload.get('secret_like_reference_count')}`",
        "",
        "## Files",
    ]
    for row in files:
        warnings = ", ".join(row.get("warnings") or [])
        errors = ", ".join(row.get("errors") or [])
        lines.append(
            f"- `{row.get('source')}` status=`{row.get('status')}` warnings=`{warnings or '-'}` errors=`{errors or '-'}`"
        )
        for leak in row.get("secret_findings") or row.get("secret_leaks") or []:
            if isinstance(leak, dict):
                lines.append(
                    f"  - {leak.get('kind', 'secret_finding')} severity=`{leak.get('severity', '-')}` "
                    f"path=`{leak.get('field_path') or leak.get('path')}` masked=`{leak.get('masked_preview') or leak.get('masked_value') or '****'}` "
                    f"reason=`{leak.get('reason') or '-'}`"
                )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Missing result files are warnings, not program failures.",
            "- `network_disabled` belongs to skipped/offline diagnostics, not provider failure.",
            "- `confirmed_secret_leak_count` is the high-risk count. `secret_like_reference_count` covers paths, examples, placeholders, masked values, and documentation text.",
            "- Secret checks mask values and do not read `config/llm_config.json` or `config/provider_secrets.json`.",
        ]
    )
    return "\n".join(lines)
