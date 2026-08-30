from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quantdesk.shared import now_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JSON = PROJECT_ROOT / "results" / "v38_cleanup_audit_latest.json"
DEFAULT_REPORT = PROJECT_ROOT / "reports" / "v38_cleanup_audit_latest.md"
DEFAULT_MANIFEST_JSON = PROJECT_ROOT / "results" / "v38_cleanup_safety_manifest_latest.json"
DEFAULT_MANIFEST_REPORT = PROJECT_ROOT / "reports" / "v38_cleanup_safety_manifest_latest.md"

PLANNED_RUNTIME_DIRS = [
    "quantdesk/reports",
    "quantdesk/results",
    "quantdesk/state",
]
PLANNED_ROOT_SHIMS = [
    "quantdesk/strategy_recommender.py",
    "quantdesk/auto_paper_portfolio.py",
    "quantdesk/position_management_engine.py",
]
PLANNED_MICRO_SCRIPTS = [
    "scripts/run_smart_stop.py",
    "scripts/run_similar_day.py",
    "scripts/run_stress_test.py",
    "scripts/run_style_attribution.py",
    "scripts/run_decision_journal.py",
    "scripts/run_dragon_tiger.py",
    "scripts/run_dynamic_factor_model.py",
    "scripts/run_execution_realism.py",
    "scripts/run_fundamental_metrics.py",
    "scripts/run_local_store.py",
    "scripts/run_money_flow.py",
    "scripts/run_notification_center.py",
    "scripts/run_paper_watchlist.py",
    "scripts/run_return_explain.py",
    "scripts/run_risk_alerts.py",
    "scripts/run_stock_picking_flow.py",
    "scripts/run_daily_candidate_review.py",
]

SCRIPT_COMMANDS = {
    "run_smart_stop.py": "smart-stop",
    "run_similar_day.py": "similar-day",
    "run_stress_test.py": "stress-test",
    "run_style_attribution.py": "style-attribution",
    "run_decision_journal.py": "decision-journal",
    "run_dragon_tiger.py": "dragon-tiger",
    "run_dynamic_factor_model.py": "dynamic-factor-model",
    "run_execution_realism.py": "execution-realism",
    "run_fundamental_metrics.py": "fundamental-metrics",
    "run_local_store.py": "local-store",
    "run_money_flow.py": "money-flow",
    "run_notification_center.py": "notify",
    "run_paper_watchlist.py": "paper-watchlist",
    "run_return_explain.py": "return-explain",
    "run_risk_alerts.py": "risk-alerts",
    "run_stock_picking_flow.py": "stock-picking-flow",
    "run_daily_candidate_review.py": "candidate-review",
}


def _rel(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _read_qd_text() -> str:
    path = PROJECT_ROOT / "scripts" / "qd.py"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def audit_runtime_dirs(paths: list[str] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in paths or PLANNED_RUNTIME_DIRS:
        path = PROJECT_ROOT / raw
        if not path.exists():
            rows.append(
                {
                    "path": raw,
                    "exists": False,
                    "file_count": 0,
                    "classification": "already_absent",
                    "recommendation": "no_action",
                    "delete_safe": False,
                }
            )
            continue
        files = [item for item in path.rglob("*") if item.is_file()]
        rows.append(
            {
                "path": raw,
                "exists": True,
                "file_count": len(files),
                "sample_files": [_rel(item) for item in files[:10]],
                "classification": "runtime_output_dir_non_empty" if files else "empty_runtime_dir",
                "recommendation": "do_not_delete_current_workspace" if files else "manual_review_before_delete",
                "delete_safe": False,
            }
        )
    return rows


def audit_root_shims(paths: list[str] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in paths or PLANNED_ROOT_SHIMS:
        path = PROJECT_ROOT / raw
        text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
        is_shim = "Compatibility shim" in text and "import *" in text
        classification = "compatibility_shim" if is_shim else "active_real_module"
        if not path.exists():
            rows.append(
                {
                    "path": raw,
                    "exists": False,
                    "line_count": 0,
                    "classification": "removed_as_planned",
                    "recommendation": "no_action",
                    "delete_safe": False,
                }
            )
            continue
        rows.append(
            {
                "path": raw,
                "exists": path.exists(),
                "line_count": len(text.splitlines()) if text else 0,
                "classification": classification,
                "recommendation": "keep_until_import_alias_audit_passes" if is_shim else "keep_active_module",
                "delete_safe": False,
            }
        )
    return rows


def audit_micro_scripts(paths: list[str] | None = None) -> list[dict[str, Any]]:
    qd_text = _read_qd_text()
    rows: list[dict[str, Any]] = []
    for raw in paths or PLANNED_MICRO_SCRIPTS:
        path = PROJECT_ROOT / raw
        script_name = path.name
        command = SCRIPT_COMMANDS.get(script_name, "")
        qd_has_script = script_name in qd_text
        qd_has_command = bool(command and f'"{command}"' in qd_text)
        if not path.exists():
            rows.append(
                {
                    "path": raw,
                    "exists": False,
                    "size_bytes": 0,
                    "qd_command": command,
                    "qd_has_script_reference": qd_has_script,
                    "qd_has_command": qd_has_command,
                    "classification": "removed_qd_command_covered" if qd_has_command else "removed_missing_qd_command",
                    "recommendation": "no_action" if qd_has_command else "restore_qd_command_alias",
                    "delete_safe": False,
                }
            )
            continue
        rows.append(
            {
                "path": raw,
                "exists": path.exists(),
                "size_bytes": path.stat().st_size if path.exists() else 0,
                "qd_command": command,
                "qd_has_script_reference": qd_has_script,
                "qd_has_command": qd_has_command,
                "classification": "micro_script_qd_covered" if qd_has_script and qd_has_command else "micro_script_needs_alias_review",
                "recommendation": "defer_delete_keep_compatibility" if qd_has_script and qd_has_command else "add_or_fix_qd_alias_before_delete",
                "delete_safe": False,
            }
        )
    return rows


def build_v38_cleanup_audit() -> dict[str, Any]:
    runtime_dirs = audit_runtime_dirs()
    root_shims = audit_root_shims()
    micro_scripts = audit_micro_scripts()
    blockers = []
    if any(row["classification"] == "runtime_output_dir_non_empty" for row in runtime_dirs):
        blockers.append("planned_runtime_dirs_are_not_empty")
    if any(row["classification"] not in {"compatibility_shim", "removed_as_planned", "active_real_module"} for row in root_shims):
        blockers.append("root_shim_manual_review_required")
    if any(row["classification"] not in {"micro_script_qd_covered", "removed_qd_command_covered"} for row in micro_scripts):
        blockers.append("micro_script_alias_review_required")
    delete_candidates = [
        row["path"]
        for row in [*root_shims, *micro_scripts]
        if row.get("classification") in {"compatibility_shim", "micro_script_qd_covered"}
    ]
    safety_manifest = build_cleanup_safety_manifest(runtime_dirs, root_shims, micro_scripts, delete_candidates)
    return {
        "task_id": "v38_cleanup_audit",
        "status": "warning" if blockers else "passed",
        "research_only": True,
        "no_files_deleted": True,
        "generated_at": now_text(),
        "runtime_dirs": runtime_dirs,
        "root_shims": root_shims,
        "micro_scripts": micro_scripts,
        "cleanup_safety_manifest": safety_manifest,
        "delete_candidates_after_manual_review": delete_candidates,
        "blockers": blockers,
        "recommendation": (
            "do_not_delete_runtime_dirs; keep shims and micro scripts until import/CLI compatibility audit is reviewed"
            if blockers
            else "manual_review_can_consider_cleanup"
        ),
        "warnings": blockers,
        "disclaimer": "Audit only. This command does not delete files, modify git state, package, release, or trade.",
    }


def build_cleanup_safety_manifest(
    runtime_dirs: list[dict[str, Any]] | None = None,
    root_shims: list[dict[str, Any]] | None = None,
    micro_scripts: list[dict[str, Any]] | None = None,
    delete_candidates: list[str] | None = None,
) -> dict[str, Any]:
    runtime_dirs = runtime_dirs if runtime_dirs is not None else audit_runtime_dirs()
    root_shims = root_shims if root_shims is not None else audit_root_shims()
    micro_scripts = micro_scripts if micro_scripts is not None else audit_micro_scripts()
    delete_candidates = delete_candidates if delete_candidates is not None else [
        row["path"]
        for row in [*root_shims, *micro_scripts]
        if row.get("classification") in {"compatibility_shim", "micro_script_qd_covered"}
    ]
    runtime_retention = [
        {
            "path": row["path"],
            "file_count": row.get("file_count", 0),
            "reason": "non_empty_runtime_outputs_in_current_workspace"
            if row.get("classification") == "runtime_output_dir_non_empty"
            else "not_present_or_empty",
            "automatic_delete_allowed": False,
        }
        for row in runtime_dirs
    ]
    shim_retention = [
        {
                "path": row["path"],
                "classification": row.get("classification"),
                "reason": "already_removed_as_planned"
                if row.get("classification") == "removed_as_planned"
                else "compatibility_alias_requires_import_alias_review"
                if row.get("classification") == "compatibility_shim"
                else "manual_review_required",
                "automatic_delete_allowed": False,
            }
        for row in root_shims
    ]
    script_retention = [
        {
            "path": row["path"],
                "qd_command": row.get("qd_command"),
                "classification": row.get("classification"),
                "reason": "already_removed_qd_command_covered"
                if row.get("classification") == "removed_qd_command_covered"
                else "qd_alias_exists_but_script_kept_for_backwards_compatible_entrypoint",
                "automatic_delete_allowed": False,
            }
        for row in micro_scripts
    ]
    has_non_empty_runtime_dirs = any(row.get("file_count", 0) for row in runtime_retention)
    cleanup_applied = (
        not has_non_empty_runtime_dirs
        and all(row.get("classification") == "removed_as_planned" for row in root_shims)
        and all(row.get("classification") == "removed_qd_command_covered" for row in micro_scripts)
    )
    return {
        "task_id": "v38_cleanup_safety_manifest",
        "status": "cleanup_applied_no_delete_candidates"
        if cleanup_applied
        else "manual_review_required"
        if has_non_empty_runtime_dirs
        else "ready_for_manual_cleanup_review",
        "research_only": True,
        "no_files_deleted": True,
        "generated_at": now_text(),
        "automatic_cleanup_allowed": False,
        "runtime_retention": runtime_retention,
        "compatibility_shim_retention": shim_retention,
        "micro_script_retention": script_retention,
        "manual_review_delete_candidates": delete_candidates,
        "manual_review_required_before_delete": not cleanup_applied,
        "recommended_next_action": (
            "no cleanup delete candidates remain; keep qd command coverage and audit outputs"
            if cleanup_applied
            else
            "move_or_archive_runtime_outputs_outside_quantdesk_package_before deleting directories"
            if has_non_empty_runtime_dirs
            else "human reviewer may decide whether compatibility shims and tiny scripts can be removed"
        ),
        "disclaimer": "Cleanup safety manifest only. It does not delete files, package, release, trade, or modify data/daily.",
    }


def render_v38_cleanup_audit_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# v3.8 Cleanup Audit",
        "",
        "Audit only. No files were deleted.",
        "",
        f"- status: `{payload.get('status')}`",
        f"- no_files_deleted: `{payload.get('no_files_deleted')}`",
        f"- recommendation: `{payload.get('recommendation')}`",
        "",
        "## Runtime Dirs",
    ]
    for row in payload.get("runtime_dirs", []):
        lines.append(f"- `{row['path']}`: {row['classification']}, files={row.get('file_count', 0)}, recommendation={row['recommendation']}")
    lines.append("")
    lines.append("## Root Shims")
    for row in payload.get("root_shims", []):
        lines.append(f"- `{row['path']}`: {row['classification']}, recommendation={row['recommendation']}")
    lines.append("")
    lines.append("## Micro Scripts")
    for row in payload.get("micro_scripts", []):
        lines.append(f"- `{row['path']}` -> `{row.get('qd_command')}`: {row['classification']}")
    lines.append("")
    lines.append("## Blockers")
    blockers = payload.get("blockers") or []
    lines.extend([f"- {item}" for item in blockers] if blockers else ["- none"])
    lines.append("")
    lines.append("## Cleanup Safety Gate")
    manifest = payload.get("cleanup_safety_manifest") or {}
    lines.append(f"- status: `{manifest.get('status')}`")
    lines.append(f"- automatic_cleanup_allowed: `{manifest.get('automatic_cleanup_allowed')}`")
    lines.append(f"- manual_review_required_before_delete: `{manifest.get('manual_review_required_before_delete')}`")
    lines.append(f"- recommended_next_action: `{manifest.get('recommended_next_action')}`")
    lines.append("")
    lines.append(str(payload.get("disclaimer") or ""))
    return "\n".join(lines) + "\n"


def render_cleanup_safety_manifest_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# v3.8 Cleanup Safety Manifest",
        "",
        "This manifest is a dry-run safety gate. No files were deleted.",
        "",
        f"- status: `{payload.get('status')}`",
        f"- automatic_cleanup_allowed: `{payload.get('automatic_cleanup_allowed')}`",
        f"- manual_review_required_before_delete: `{payload.get('manual_review_required_before_delete')}`",
        f"- recommended_next_action: `{payload.get('recommended_next_action')}`",
        "",
        "## Runtime Retention",
    ]
    for row in payload.get("runtime_retention", []):
        lines.append(f"- `{row['path']}`: files={row.get('file_count', 0)}, reason={row.get('reason')}")
    lines.extend(["", "## Compatibility Shims"])
    for row in payload.get("compatibility_shim_retention", []):
        lines.append(f"- `{row['path']}`: {row.get('reason')}")
    lines.extend(["", "## Micro Scripts"])
    for row in payload.get("micro_script_retention", []):
        lines.append(f"- `{row['path']}` -> `{row.get('qd_command')}`: {row.get('reason')}")
    lines.extend(["", "## Manual Review Delete Candidates"])
    lines.extend([f"- {item}" for item in payload.get("manual_review_delete_candidates", [])] or ["- none"])
    lines.extend(["", str(payload.get("disclaimer") or "")])
    return "\n".join(lines) + "\n"


def write_v38_cleanup_audit(
    payload: dict[str, Any],
    *,
    output_json: str | Path = DEFAULT_JSON,
    report_md: str | Path = DEFAULT_REPORT,
) -> dict[str, str]:
    json_path = Path(output_json)
    md_path = Path(report_md)
    if not json_path.is_absolute():
        json_path = PROJECT_ROOT / json_path
    if not md_path.is_absolute():
        md_path = PROJECT_ROOT / md_path
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    md_path.write_text(render_v38_cleanup_audit_markdown(payload), encoding="utf-8")
    return {"json_path": str(json_path), "report_path": str(md_path)}


def write_cleanup_safety_manifest(
    payload: dict[str, Any],
    *,
    output_json: str | Path = DEFAULT_MANIFEST_JSON,
    report_md: str | Path = DEFAULT_MANIFEST_REPORT,
) -> dict[str, str]:
    json_path = Path(output_json)
    md_path = Path(report_md)
    if not json_path.is_absolute():
        json_path = PROJECT_ROOT / json_path
    if not md_path.is_absolute():
        md_path = PROJECT_ROOT / md_path
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    md_path.write_text(render_cleanup_safety_manifest_markdown(payload), encoding="utf-8")
    return {"json_path": str(json_path), "report_path": str(md_path)}
