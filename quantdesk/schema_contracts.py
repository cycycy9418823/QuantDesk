"""Lightweight JSON result contracts for QuantDesk.

The helpers here standardize the common outer shell of result payloads without
forcing a large migration.  Existing modules can keep their domain-specific
fields while using these functions to normalize status, warnings, version
metadata, and safety disclaimers.
"""

from __future__ import annotations

from typing import Any

from quantdesk.shared import ensure_list, now_text, short_error_text
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

ALLOWED_STATUSES = {
    "passed",
    "warning",
    "failed",
    "skipped",
    "missing",
    "partial",
    "unavailable",
}
STATUS_ALIASES = {
    "ok": "passed",
    "success": "passed",
    "true": "passed",
    "error": "failed",
    "exception": "failed",
    "not_available": "unavailable",
    "not_implemented": "unavailable",
    "notavailable": "unavailable",
}
DEFAULT_DISCLAIMER = "仅用于本地学习研究，不构成投资建议。"


def normalize_status(value: Any, *, default: str = "warning") -> str:
    text = str(value if value is not None else default).strip().lower()
    if not text:
        text = default
    text = STATUS_ALIASES.get(text, text)
    return text if text in ALLOWED_STATUSES else str(default or "warning")


def normalize_warnings(value: Any) -> list[str]:
    warnings: list[str] = []
    for item in ensure_list(value):
        if item is None or item == "":
            continue
        warnings.append(short_error_text(item))
    return warnings


def normalize_disclaimer(value: Any = None) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_DISCLAIMER


def ensure_schema_version(payload: dict[str, Any] | None, schema_version: str | None = None) -> dict[str, Any]:
    out = dict(payload or {})
    out.setdefault("schema_version", schema_version or API_SCHEMA_VERSION)
    out.setdefault("api_schema_version", schema_version or API_SCHEMA_VERSION)
    return out


def ensure_app_metadata(payload: dict[str, Any] | None) -> dict[str, Any]:
    out = ensure_schema_version(payload)
    out.setdefault("app_version", APP_VERSION)
    out.setdefault("package_version", PACKAGE_VERSION)
    out.setdefault("api_schema_version", API_SCHEMA_VERSION)
    return out


def build_base_result(
    *,
    status: Any = "passed",
    ok: bool | None = None,
    target_date: str = "",
    symbols: Any = None,
    warnings: Any = None,
    failed_reason: str = "",
    skipped_reason: str = "",
    suggested_next_action: str = "",
    disclaimer: str = "",
    generated_at: str = "",
    **extra: Any,
) -> dict[str, Any]:
    normalized_status = normalize_status(status)
    normalized_warnings = normalize_warnings(warnings)
    if ok is None:
        ok = normalized_status not in {"failed"}
    result = {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "status": normalized_status,
        "ok": bool(ok),
        "generated_at": generated_at or now_text(),
        "target_date": str(target_date or ""),
        "symbols": [str(item) for item in ensure_list(symbols) if str(item or "").strip()],
        "warnings": normalized_warnings,
        "failed_reason": short_error_text(failed_reason),
        "skipped_reason": short_error_text(skipped_reason),
        "suggested_next_action": short_error_text(suggested_next_action, max_len=500),
        "disclaimer": normalize_disclaimer(disclaimer),
    }
    result.update(extra)
    return result


def build_warning_result(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("status", "warning")
    kwargs.setdefault("ok", True)
    return build_base_result(**kwargs)


def build_error_result(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("status", "failed")
    kwargs.setdefault("ok", False)
    return build_base_result(**kwargs)


def validate_result_has_required_fields(payload: dict[str, Any], required_fields: list[str] | None = None) -> dict[str, Any]:
    required = required_fields or [
        "schema_version",
        "app_version",
        "status",
        "warnings",
        "disclaimer",
    ]
    missing = [field for field in required if field not in (payload or {})]
    return {
        "ok": not missing,
        "status": "passed" if not missing else "warning",
        "missing_required_fields": missing,
        "warnings": [f"missing required field: {field}" for field in missing],
    }


def validate_base_result_contract(payload: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "status": "failed",
            "warnings": [],
            "errors": ["payload_not_dict"],
        }
    required = validate_result_has_required_fields(payload)
    warnings.extend(required.get("warnings") or [])
    raw_status = str(payload.get("status") or "").strip().lower()
    if raw_status and raw_status not in ALLOWED_STATUSES and raw_status not in STATUS_ALIASES:
        errors.append(f"illegal status: {short_error_text(raw_status)}")
    if "warnings" in payload and not isinstance(payload.get("warnings"), list):
        warnings.append("warnings field must be a list")
    if not payload.get("generated_at") and not payload.get("checked_at"):
        warnings.append("missing generated_at or checked_at")
    status = "failed" if errors else ("warning" if warnings else "passed")
    return {
        "ok": not errors,
        "status": status,
        "warnings": warnings,
        "errors": errors,
    }


def summarize_contract_validation(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": validation.get("status") or "unknown",
        "ok": bool(validation.get("ok")),
        "warning_count": len(validation.get("warnings") or []),
        "error_count": len(validation.get("errors") or []),
        "warnings": validation.get("warnings") or [],
        "errors": validation.get("errors") or [],
    }
