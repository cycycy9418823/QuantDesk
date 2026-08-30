"""Audit records for QuantDesk observation reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from quantdesk.agent.agent_hallucination_guard import validate_report_grounding
from quantdesk.agent.agent_policy_guard import validate_agent_allowed_output
from quantdesk.shared import now_text, short_error_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

SCHEMA_VERSION = API_SCHEMA_VERSION
DISCLAIMER = "Local learning research only. Not investment advice."
DEFAULT_AUDIT_FILENAME = "strategy_observation_report_audit_latest.json"
SECRET_KEYWORDS = ("api_key", "apikey", "token", "secret", "password", "credential")


def _looks_secret_key(key: Any) -> bool:
    text = str(key or "").lower()
    return any(marker in text for marker in SECRET_KEYWORDS)


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        clean: dict[str, Any] = {}
        for key, value in obj.items():
            if _looks_secret_key(key):
                clean[str(key)] = "[redacted]"
            else:
                clean[str(key)] = _redact(value)
        return clean
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    if isinstance(obj, tuple):
        return [_redact(item) for item in obj]
    text = str(obj) if obj is not None else ""
    if any(marker in text.lower() for marker in SECRET_KEYWORDS):
        return "[redacted]"
    return obj


def compute_safe_hash(obj_or_text: Any) -> str:
    """Return a SHA-256 hash of a redacted stable representation."""

    redacted = _redact(obj_or_text)
    if isinstance(redacted, str):
        payload = redacted
    else:
        payload = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def audit_report_quality(report_text: str, context: dict[str, Any], evidence_pack: dict[str, Any]) -> dict[str, Any]:
    policy = validate_agent_allowed_output(report_text)
    grounding = validate_report_grounding(report_text, evidence_pack)
    return {
        "ok": bool(policy.get("ok")) and bool(grounding.get("ok")),
        "policy_guard_status": "passed" if policy.get("ok") else "blocked",
        "grounding_guard_status": grounding.get("status"),
        "grounding_score": grounding.get("grounding_score"),
        "unsupported_issue_count": grounding.get("issue_count"),
        "unsupported_issues": grounding.get("issues") or [],
        "policy_violations": policy.get("violations") or [],
        "context_hash": compute_safe_hash(context),
        "evidence_pack_hash": compute_safe_hash(evidence_pack),
        "report_hash": compute_safe_hash(report_text),
        "warnings": [short_error_text(item) for item in grounding.get("warnings", [])],
    }


def build_report_audit_record(
    context: dict[str, Any],
    evidence_pack: dict[str, Any],
    llm_result: dict[str, Any],
) -> dict[str, Any]:
    report_text = str((llm_result or {}).get("report") or "")
    quality = audit_report_quality(report_text, context, evidence_pack)
    status = "passed" if quality.get("ok") else "warning"
    prompt_hash = str((llm_result or {}).get("prompt_hash") or compute_safe_hash((llm_result or {}).get("prompt") or ""))
    record = {
        "schema_version": SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "status": status,
        "ok": bool(quality.get("ok")),
        "generated_at": now_text(),
        "symbol": str((context or {}).get("symbol") or (evidence_pack or {}).get("symbol") or ""),
        "date": str((context or {}).get("date") or (evidence_pack or {}).get("date") or ""),
        "llm_provider": (llm_result or {}).get("provider") or "unknown",
        "llm_used": bool((llm_result or {}).get("llm_used")),
        "fallback_used": bool((llm_result or {}).get("fallback_used")),
        "validation_summary": (context or {}).get("validation_summary") if isinstance((context or {}).get("validation_summary"), dict) else {},
        "data_health_summary": (context or {}).get("data_health_summary") if isinstance((context or {}).get("data_health_summary"), dict) else {},
        "data_health_summary_hash": compute_safe_hash((context or {}).get("data_health_summary") or {}),
        "feature_quality_summary": (context or {}).get("feature_quality_summary") if isinstance((context or {}).get("feature_quality_summary"), dict) else {},
        "statistical_validation_summary": (context or {}).get("statistical_validation_summary") if isinstance((context or {}).get("statistical_validation_summary"), dict) else {},
        "parameter_sensitivity_summary": (context or {}).get("parameter_sensitivity_summary") if isinstance((context or {}).get("parameter_sensitivity_summary"), dict) else {},
        "parameter_sensitivity_summary_hash": compute_safe_hash((context or {}).get("parameter_sensitivity_summary") or {}),
        "policy_guard_status": (llm_result or {}).get("policy_guard_status") or quality.get("policy_guard_status"),
        "grounding_guard_status": (llm_result or {}).get("grounding_guard_status") or quality.get("grounding_guard_status"),
        "grounding_score": (llm_result or {}).get("grounding_score", quality.get("grounding_score")),
        "unsupported_issue_count": (llm_result or {}).get(
            "unsupported_issue_count", quality.get("unsupported_issue_count")
        ),
        "unsupported_issues": (llm_result or {}).get("unsupported_issues") or quality.get("unsupported_issues"),
        "context_hash": (llm_result or {}).get("context_hash") or quality.get("context_hash"),
        "evidence_pack_hash": (llm_result or {}).get("evidence_pack_hash") or quality.get("evidence_pack_hash"),
        "prompt_hash": prompt_hash,
        "report_hash": (llm_result or {}).get("report_hash") or quality.get("report_hash"),
        "warnings": [
            short_error_text(item)
            for item in [
                *((llm_result or {}).get("warnings") or []),
                *(quality.get("warnings") or []),
            ]
            if item
        ],
        "disclaimer": DISCLAIMER,
    }
    return _redact(record)


def write_report_audit(record: dict[str, Any], output_path: str | Path) -> dict[str, Any]:
    path = Path(output_path)
    write_json(path, _redact(record))
    return {"ok": True, "path": str(path), "record": _redact(record)}


def summarize_report_audit(record: dict[str, Any]) -> str:
    return (
        "Report audit: "
        f"policy={record.get('policy_guard_status') or '-'}, "
        f"grounding={record.get('grounding_guard_status') or '-'}, "
        f"score={record.get('grounding_score')}, "
        f"unsupported={record.get('unsupported_issue_count')}"
    )
