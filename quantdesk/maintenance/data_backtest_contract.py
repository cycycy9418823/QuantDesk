from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.reproducibility_manifest import compute_file_hash
from quantdesk.shared import read_json, write_json, write_md
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "maintenance" / "data_backtest_contract_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "maintenance" / "data_backtest_contract_latest.md"
BACKTEST_JSON = PROJECT_ROOT / "results" / "backtest" / "backtest_latest.json"
RETENTION_MANIFEST = PROJECT_ROOT / "results" / "daily_retention" / "manifest_check_latest.json"
DISCLAIMER = (
    "Read-only data/backtest contract inventory. It audits existing local output metadata and file hashes; "
    "it does not rerun backtests, update data, enable network providers, connect brokers, or place real orders."
)

REQUIRED_BACKTEST_TOP_LEVEL = [
    "task_type",
    "status",
    "network",
    "full_scan",
    "symbols",
    "metrics",
    "assumptions",
    "data_gate",
    "pit_filter_audit",
    "artifact_paths",
    "output_paths",
    "disclaimer",
]
REQUIRED_ASSUMPTIONS = [
    "data_source",
    "commission_rate",
    "stamp_duty_rate",
    "slippage_bps",
    "execution_price",
    "execution_date",
    "costs_implemented",
    "limit_up_rule",
    "limit_down_rule",
    "suspension_rule",
    "pit_universe_filter",
    "pit_universe_used",
    "not_investment_advice",
    "no_broker_connection",
    "no_real_orders",
]
REQUIRED_METADATA = [
    "survivorship_bias_warning",
    "research_only",
    "execution_lag_days",
    "data_scope",
]


def _rel(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    target = Path(path)
    if not target.is_absolute():
        target = root / target
    try:
        return str(target.relative_to(root)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _project_path(path: str | Path, *, root: Path = PROJECT_ROOT) -> Path:
    target = Path(path)
    return target if target.is_absolute() else root / target


def _hash_records(paths: dict[str, Any], *, root: Path = PROJECT_ROOT) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for name, raw_path in paths.items():
        if not raw_path:
            continue
        target = _project_path(str(raw_path), root=root)
        if target.exists() and target.is_file():
            records[str(name)] = compute_file_hash(target)
        else:
            records[str(name)] = {"path": _rel(target, root=root), "exists": False, "sha256": "", "size_bytes": 0}
    return records


def _backtest_contract(payload: dict[str, Any], *, root: Path = PROJECT_ROOT, path: Path = BACKTEST_JSON) -> dict[str, Any]:
    if not payload:
        return {
            "status": "missing",
            "exists": False,
            "path": _rel(path, root=root),
            "missing_top_level_fields": REQUIRED_BACKTEST_TOP_LEVEL,
            "missing_assumption_fields": REQUIRED_ASSUMPTIONS,
            "missing_assumption_metadata_fields": REQUIRED_METADATA,
            "artifact_records": {},
            "artifact_missing": [],
            "warnings": ["backtest_latest_missing"],
        }
    assumptions = payload.get("assumptions") if isinstance(payload.get("assumptions"), dict) else {}
    metadata = assumptions.get("assumption_metadata") if isinstance(assumptions.get("assumption_metadata"), dict) else {}
    artifact_paths = payload.get("artifact_paths") if isinstance(payload.get("artifact_paths"), dict) else {}
    output_paths = payload.get("output_paths") if isinstance(payload.get("output_paths"), dict) else {}
    file_records = _hash_records({**artifact_paths, **output_paths, "latest_json": _rel(path, root=root)}, root=root)
    artifact_missing = [
        record.get("path")
        for record in file_records.values()
        if not record.get("exists")
    ]
    missing_top = [key for key in REQUIRED_BACKTEST_TOP_LEVEL if key not in payload]
    missing_assumptions = [key for key in REQUIRED_ASSUMPTIONS if key not in assumptions]
    missing_metadata = [key for key in REQUIRED_METADATA if key not in metadata]
    warnings: list[str] = []
    if missing_top:
        warnings.append("backtest_missing_top_level_fields")
    if missing_assumptions:
        warnings.append("backtest_missing_assumption_fields")
    if missing_metadata:
        warnings.append("backtest_missing_assumption_metadata_fields")
    if artifact_missing:
        warnings.append("backtest_artifact_paths_missing")
    if payload.get("network") is not False:
        warnings.append("backtest_network_not_false")
    if payload.get("full_scan") is not False:
        warnings.append("backtest_full_scan_not_false")
    if assumptions.get("costs_implemented") is not True:
        warnings.append("backtest_costs_not_marked_implemented")
    if metadata.get("survivorship_bias_warning") is not True:
        warnings.append("survivorship_bias_warning_missing_or_false")
    if not payload.get("pit_filter_audit"):
        warnings.append("pit_filter_audit_missing")
    if not payload.get("data_gate"):
        warnings.append("data_gate_missing")
    status = "warning" if warnings else "passed"
    return {
        "status": status,
        "exists": True,
        "path": _rel(path, root=root),
        "task_type": payload.get("task_type"),
        "backtest_status": payload.get("status"),
        "network": payload.get("network"),
        "full_scan": payload.get("full_scan"),
        "symbols": payload.get("symbols") or [],
        "strategy": payload.get("strategy"),
        "mode": payload.get("mode"),
        "generated_at": payload.get("generated_at"),
        "data_gate_status": (payload.get("data_gate") or {}).get("status") if isinstance(payload.get("data_gate"), dict) else "missing",
        "pit_universe_used": assumptions.get("pit_universe_used"),
        "pit_filter_audit_status": "present" if isinstance(payload.get("pit_filter_audit"), dict) else "missing",
        "cost_fields": {
            "commission_rate": assumptions.get("commission_rate"),
            "stamp_duty_rate": assumptions.get("stamp_duty_rate"),
            "slippage_bps": assumptions.get("slippage_bps"),
            "costs_implemented": assumptions.get("costs_implemented"),
        },
        "risk_metadata": {
            "survivorship_bias_warning": metadata.get("survivorship_bias_warning"),
            "research_only": metadata.get("research_only"),
            "execution_lag_days": metadata.get("execution_lag_days"),
            "data_scope": metadata.get("data_scope"),
            "risk_flags": assumptions.get("risk_flags") or [],
        },
        "missing_top_level_fields": missing_top,
        "missing_assumption_fields": missing_assumptions,
        "missing_assumption_metadata_fields": missing_metadata,
        "artifact_records": file_records,
        "artifact_missing": artifact_missing,
        "artifact_count": len(file_records),
        "warnings": warnings,
    }


def _manifest_contract(payload: dict[str, Any], *, root: Path = PROJECT_ROOT, path: Path = RETENTION_MANIFEST) -> dict[str, Any]:
    if not payload:
        return {
            "status": "missing",
            "exists": False,
            "path": _rel(path, root=root),
            "validation_status": "missing",
            "hash_mismatches": [],
            "warnings": ["retention_manifest_check_missing"],
        }
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}
    warnings: list[str] = []
    if validation.get("status") != "passed":
        warnings.append("retention_manifest_validation_not_passed")
    if validation.get("hash_mismatches"):
        warnings.append("retention_manifest_hash_mismatch")
    if payload.get("network") is not False:
        warnings.append("retention_manifest_network_not_false")
    return {
        "status": "warning" if warnings else "passed",
        "exists": True,
        "path": _rel(path, root=root),
        "manifest": payload.get("manifest"),
        "validation_status": validation.get("status") or "missing",
        "missing_required_fields": validation.get("missing_required_fields") or [],
        "hash_mismatches": validation.get("hash_mismatches") or [],
        "network": payload.get("network"),
        "llm_used": payload.get("llm_used"),
        "warnings": warnings,
    }


def build_data_backtest_contract(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    root = Path(project_root)
    backtest_path = root / "results" / "backtest" / "backtest_latest.json"
    manifest_path = root / "results" / "daily_retention" / "manifest_check_latest.json"
    backtest = _backtest_contract(read_json(backtest_path, {}), root=root, path=backtest_path)
    manifest = _manifest_contract(read_json(manifest_path, {}), root=root, path=manifest_path)
    warnings = []
    warnings.extend(f"backtest:{item}" for item in backtest.get("warnings") or [])
    warnings.extend(f"manifest:{item}" for item in manifest.get("warnings") or [])
    payload: dict[str, Any] = {
        "ok": True,
        "status": "warning" if warnings else "passed",
        "task_type": "data_backtest_contract",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "backtest": backtest,
        "retention_manifest": manifest,
        "warnings": warnings,
        "warning_count": len(warnings),
        "recommendations": [
            "Refresh the local backtest after local data changes so artifact hashes and assumptions are current.",
            "Run qd.py daily-retain or manifest-check before release discussions that depend on reproducibility.",
            "Keep PIT and survivorship-bias warnings visible unless formal historical listings are available.",
            "Do not treat missing benchmarks, missing PIT data, or missing artifact hashes as real evidence.",
        ],
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return payload


def render_data_backtest_contract_markdown(payload: dict[str, Any]) -> str:
    backtest = payload.get("backtest") if isinstance(payload.get("backtest"), dict) else {}
    manifest = payload.get("retention_manifest") if isinstance(payload.get("retention_manifest"), dict) else {}
    lines = [
        "# Data Backtest Contract",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- backtest_status: `{backtest.get('status')}`",
        f"- manifest_status: `{manifest.get('status')}`",
        f"- backtest_artifact_count: `{backtest.get('artifact_count', 0)}`",
        f"- backtest_artifact_missing: `{len(backtest.get('artifact_missing') or [])}`",
        "",
        "## Backtest Fields",
        "",
        f"- missing_top_level_fields: `{backtest.get('missing_top_level_fields') or []}`",
        f"- missing_assumption_fields: `{backtest.get('missing_assumption_fields') or []}`",
        f"- missing_assumption_metadata_fields: `{backtest.get('missing_assumption_metadata_fields') or []}`",
        f"- cost_fields: `{backtest.get('cost_fields') or {}}`",
        f"- risk_metadata: `{backtest.get('risk_metadata') or {}}`",
        "",
        "## Manifest",
        "",
        f"- validation_status: `{manifest.get('validation_status')}`",
        f"- hash_mismatches: `{manifest.get('hash_mismatches') or []}`",
        "",
        "## Warnings",
        "",
    ]
    lines.extend([f"- {item}" for item in payload.get("warnings") or []] if payload.get("warnings") else ["- none"])
    lines.extend(["", "## Recommendations", ""])
    lines.extend([f"- {item}" for item in payload.get("recommendations") or []] or ["- none"])
    return "\n".join(lines) + "\n"


def write_data_backtest_contract_outputs(
    payload: dict[str, Any] | None = None,
    *,
    result_path: str | Path = RESULT_JSON,
    report_path: str | Path = REPORT_MD,
) -> dict[str, Any]:
    payload = dict(payload or build_data_backtest_contract())
    result = Path(result_path)
    report = Path(report_path)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    write_json(result, payload)
    write_md(report, render_data_backtest_contract_markdown(payload))
    return payload


def compact_data_backtest_contract_summary(payload: dict[str, Any]) -> dict[str, Any]:
    backtest = payload.get("backtest") if isinstance(payload.get("backtest"), dict) else {}
    manifest = payload.get("retention_manifest") if isinstance(payload.get("retention_manifest"), dict) else {}
    return {
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "task_type": payload.get("task_type"),
        "backtest_status": backtest.get("status"),
        "backtest_artifact_count": backtest.get("artifact_count"),
        "backtest_artifact_missing": backtest.get("artifact_missing"),
        "missing_assumption_fields": backtest.get("missing_assumption_fields"),
        "missing_assumption_metadata_fields": backtest.get("missing_assumption_metadata_fields"),
        "manifest_status": manifest.get("status"),
        "manifest_validation_status": manifest.get("validation_status"),
        "warnings": payload.get("warnings"),
        "output_paths": payload.get("output_paths"),
        "disclaimer": payload.get("disclaimer"),
    }


def dumps_payload(payload: dict[str, Any], *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
