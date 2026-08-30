"""Reproducibility manifest helpers for QuantDesk.

The manifest is a local audit artifact.  It records versions, explicit inputs,
outputs, provider traces, and file hashes, but never stores secrets or account
details.  It does not connect to the network or call any LLM.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol, now_text, short_error_text
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = API_SCHEMA_VERSION
DISCLAIMER = "仅用于本地学习研究和纸面模拟诊断，不构成投资建议。"
SECRET_FILENAMES = {"llm_config.json", "provider_secrets.json"}
SECRET_MARKERS = ("api_key", "apikey", "secret", "token", "password", "credential")


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


def _looks_secret_key(value: Any) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in SECRET_MARKERS)


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
    if _looks_secret_key(text):
        return "[redacted]"
    return obj


def compute_file_hash(path: str | Path, *, algorithm: str = "sha256") -> dict[str, Any]:
    """Return a stable file hash record.

    Secret config files are recorded only by path and existence; their contents
    are never hashed into the manifest because even hashes of low-entropy
    secrets can leak information.
    """

    target = _project_path(path)
    rel = _rel(target)
    if target.name in SECRET_FILENAMES:
        return {
            "path": rel,
            "exists": target.exists(),
            "secret_redacted": True,
            "sha256": "",
            "size_bytes": target.stat().st_size if target.exists() else 0,
        }
    if not target.exists() or not target.is_file():
        return {"path": rel, "exists": False, "sha256": "", "size_bytes": 0}
    digest = hashlib.new(algorithm)
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": rel,
        "exists": True,
        "sha256": digest.hexdigest(),
        "size_bytes": target.stat().st_size,
    }


def compute_json_hash(obj: Any) -> str:
    payload = json.dumps(_redact(obj), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_git_or_source_state() -> dict[str, Any]:
    git_dir = PROJECT_ROOT / ".git"
    return {
        "git_available": git_dir.exists(),
        "project_root_name": PROJECT_ROOT.name,
        "source_state_note": "git metadata present" if git_dir.exists() else "git metadata not available in this workspace",
    }


def capture_runtime_environment() -> dict[str, Any]:
    return {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "implementation": platform.python_implementation(),
    }


def capture_command_args(command_args: Any = None) -> list[str]:
    if command_args is None:
        return list(sys.argv)
    if isinstance(command_args, str):
        return [command_args]
    if isinstance(command_args, (list, tuple)):
        return [str(item) for item in command_args]
    return [str(command_args)]


def _normalize_symbols(symbols: Any) -> list[str]:
    if isinstance(symbols, str):
        raw = [item.strip() for item in symbols.split(",") if item.strip()]
    elif isinstance(symbols, (list, tuple, set)):
        raw = list(symbols)
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        symbol = normalize_symbol(item)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _file_records(paths: Any) -> list[dict[str, Any]]:
    if not paths:
        return []
    raw = paths if isinstance(paths, (list, tuple, set)) else [paths]
    return [compute_file_hash(item) for item in raw if str(item or "").strip()]


def build_reproducibility_manifest(
    *,
    target_date: str,
    symbols: Any,
    network: bool = False,
    llm_enabled: bool = False,
    calendar_quality: str = "",
    commands: Any = None,
    inputs: Any = None,
    outputs: Any = None,
    provider_chain: list[Any] | None = None,
    data_source_trace: Any = None,
    strategy_validation: dict[str, Any] | None = None,
    feature_quality_summary: dict[str, Any] | None = None,
    statistical_validation_summary: dict[str, Any] | None = None,
    data_health_summary: dict[str, Any] | None = None,
    parameter_sensitivity_summary: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    input_records = _file_records(inputs)
    output_records = _file_records(outputs)
    file_hashes = {record["path"]: record for record in [*input_records, *output_records]}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "status": "warning" if warnings else "passed",
        "ok": True,
        "target_date": str(target_date or ""),
        "symbols": _normalize_symbols(symbols),
        "network": bool(network),
        "llm_enabled": bool(llm_enabled),
        "calendar_quality": str(calendar_quality or ""),
        "commands": capture_command_args(commands),
        "inputs": input_records,
        "outputs": output_records,
        "file_hashes": file_hashes,
        "provider_chain": _redact(provider_chain or []),
        "data_source_trace": _redact(data_source_trace or []),
        "strategy_validation": _redact(strategy_validation or {}),
        "feature_quality_summary": _redact(feature_quality_summary or {}),
        "feature_quality_status": (feature_quality_summary or {}).get("status") if isinstance(feature_quality_summary, dict) else "missing",
        "high_missing_features": (feature_quality_summary or {}).get("high_missing_features") if isinstance(feature_quality_summary, dict) else [],
        "statistical_validation_summary": _redact(statistical_validation_summary or {}),
        "statistical_validation_status": (statistical_validation_summary or {}).get("status") if isinstance(statistical_validation_summary, dict) else "missing",
        "p_value": (statistical_validation_summary or {}).get("p_value") if isinstance(statistical_validation_summary, dict) else None,
        "bootstrap_ci_summary": (statistical_validation_summary or {}).get("bootstrap_ci_summary") if isinstance(statistical_validation_summary, dict) else {},
        "data_health_summary": _redact(data_health_summary or {}),
        "data_health_status": (data_health_summary or {}).get("status") if isinstance(data_health_summary, dict) else "missing",
        "freshness_status": (data_health_summary or {}).get("freshness_status") if isinstance(data_health_summary, dict) else "missing",
        "intraday_coverage_rate": (data_health_summary or {}).get("intraday_coverage_rate") if isinstance(data_health_summary, dict) else None,
        "parameter_sensitivity_summary": _redact(parameter_sensitivity_summary or {}),
        "parameter_sensitivity_status": (parameter_sensitivity_summary or {}).get("status") if isinstance(parameter_sensitivity_summary, dict) else "missing",
        "robustness_score": (parameter_sensitivity_summary or {}).get("robustness_score") if isinstance(parameter_sensitivity_summary, dict) else None,
        "source_state": capture_git_or_source_state(),
        "runtime_environment": capture_runtime_environment(),
        "warnings": [short_error_text(item) for item in (warnings or []) if item],
        "generated_at": generated_at or now_text(),
        "disclaimer": DISCLAIMER,
    }
    return _redact(manifest)


def validate_reproducibility_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    required = [
        "schema_version",
        "app_version",
        "package_version",
        "api_schema_version",
        "target_date",
        "symbols",
        "network",
        "calendar_quality",
        "commands",
        "inputs",
        "outputs",
        "file_hashes",
        "disclaimer",
    ]
    missing = [key for key in required if key not in manifest]
    warnings: list[str] = []
    hash_mismatches: list[dict[str, Any]] = []
    file_hashes = manifest.get("file_hashes") if isinstance(manifest.get("file_hashes"), dict) else {}
    for rel, record in file_hashes.items():
        if not isinstance(record, dict) or record.get("secret_redacted"):
            continue
        current = compute_file_hash(rel)
        if bool(record.get("exists")) != bool(current.get("exists")) or str(record.get("sha256") or "") != str(current.get("sha256") or ""):
            hash_mismatches.append({"path": rel, "expected": record.get("sha256") or "", "actual": current.get("sha256") or ""})
    text = json.dumps(manifest, ensure_ascii=False, default=str).lower()
    if "quantdesk_llm_api_key=" in text or "api key:" in text or re.search(r"(^|[^a-z0-9])sk-[a-z0-9]", text):
        warnings.append("manifest may contain secret-like text")
    ok = not missing and not hash_mismatches and not warnings
    return {
        "ok": ok,
        "status": "passed" if ok else "failed",
        "missing_required_fields": missing,
        "hash_mismatches": hash_mismatches,
        "warnings": warnings,
        "checked_at": now_text(),
    }


def compare_manifests(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    differences: list[dict[str, Any]] = []
    for key in ("app_version", "package_version", "api_schema_version", "target_date", "symbols", "network", "llm_enabled", "calendar_quality"):
        if left.get(key) != right.get(key):
            differences.append({"field": key, "left": left.get(key), "right": right.get(key)})
    for key in ("provider_chain", "data_source_trace"):
        if compute_json_hash(left.get(key)) != compute_json_hash(right.get(key)):
            differences.append({"field": key, "left_hash": compute_json_hash(left.get(key)), "right_hash": compute_json_hash(right.get(key))})
    left_inputs = {item.get("path"): item.get("sha256") for item in left.get("inputs", []) if isinstance(item, dict)}
    right_inputs = {item.get("path"): item.get("sha256") for item in right.get("inputs", []) if isinstance(item, dict)}
    if left_inputs != right_inputs:
        differences.append({"field": "inputs", "left": left_inputs, "right": right_inputs})
    return {
        "ok": not differences,
        "status": "matched" if not differences else "different",
        "difference_count": len(differences),
        "differences": differences,
        "checked_at": now_text(),
    }
