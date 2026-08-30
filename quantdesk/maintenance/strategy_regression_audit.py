from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.maintenance.import_alias_reconciliation import (
    build_import_rewrite_manifest,
    write_rewrite_manifest_outputs,
)
from quantdesk.research.strategy_viability_runner import (
    check_baseline_distinctness,
    classify_data_root_kind,
    classify_execution_evidence,
    validate_performance_metrics_sanity,
)
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "strategy_regression_audit_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "strategy_regression_audit_latest.md"

CORE_STRATEGY_FILES = [
    "quantdesk/research/strategy_viability_runner.py",
    "quantdesk/research/strategy_viability_report.py",
    "quantdesk/research/capital_lot_sensitivity.py",
    "quantdesk/research/combined_score_backtest.py",
    "quantdesk/research/factor_combiner.py",
    "scripts/run_strategy_viability_sprint.py",
    "scripts/run_capital_lot_sensitivity.py",
    "scripts/run_combined_score_backtest.py",
]


def _rel(path: str | Path) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def _changed_core_files() -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--", *CORE_STRATEGY_FILES],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]


def _status_from_bool(ok: bool) -> str:
    return "passed" if ok else "failed"


def assess_strategy_reason_codes(baselines: dict[str, Any] | None) -> dict[str, Any]:
    """Pure classifier: zero-trade vs. real homogeneity from a baselines payload.

    Prefers the runner's own ``execution_evidence`` / ``baseline_distinctness``
    verdict; for legacy reports lacking the structured field it recomputes
    execution evidence from the baseline rows so an empty-equity-curve run is
    diagnosed as ``insufficient_execution_evidence`` rather than being mislabeled
    strategy homogeneity. Never selects a best strategy.
    """

    baselines = baselines if isinstance(baselines, dict) else {}
    baseline_rows = baselines.get("baselines") or []
    execution_evidence = baselines.get("execution_evidence")
    distinctness_status_reported = baselines.get("baseline_distinctness_status")
    if baseline_rows:
        distinctness = (
            baselines.get("baseline_distinctness")
            if isinstance(baselines.get("baseline_distinctness"), dict)
            else check_baseline_distinctness(baseline_rows)
        )
    else:
        distinctness = {"status": "warning", "warnings": ["baseline_report_missing_or_empty"], "duplicated_strategy_groups": []}
    distinctness_status = str(distinctness_status_reported or distinctness.get("status") or "warning")
    if not isinstance(execution_evidence, dict) and baseline_rows:
        execution_evidence = classify_execution_evidence(baseline_rows, panel_empty=False)
    if isinstance(execution_evidence, dict):
        execution_status = str(execution_evidence.get("status") or "unknown")
        execution_reason = str(execution_evidence.get("reason") or "")
        distinctness_evaluable = bool(execution_evidence.get("distinctness_evaluable"))
    else:
        execution_status = "unknown"
        execution_reason = ""
        distinctness_evaluable = distinctness_status not in {"not_evaluable"}
    insufficient_execution_evidence = (
        distinctness_status == "not_evaluable"
        or (isinstance(execution_evidence, dict) and not distinctness_evaluable)
    )
    real_strategy_homogeneity = (
        distinctness_status == "failed" and distinctness_evaluable and not insufficient_execution_evidence
    )
    return {
        "distinctness": distinctness,
        "distinctness_status": distinctness_status,
        "execution_status": execution_status,
        "execution_reason": execution_reason,
        "distinctness_evaluable": distinctness_evaluable,
        "insufficient_execution_evidence": insufficient_execution_evidence,
        "real_strategy_homogeneity": real_strategy_homogeneity,
    }


def audit_strategy_regression() -> dict[str, Any]:
    baseline_path = PROJECT_ROOT / "results" / "strategy_viability_baselines_latest.json"
    cap_path = PROJECT_ROOT / "results" / "capital_lot_sensitivity_latest.json"
    report_path = PROJECT_ROOT / "results" / "strategy_viability_latest.json"
    baselines = _load_json(baseline_path) or {}
    capital = _load_json(cap_path) or {}
    report = _load_json(report_path) or {}
    baseline_rows = baselines.get("baselines") or []
    # Prefer the runner's own execution-evidence + distinctness verdict when
    # present (it already separates zero-trade "not_evaluable" from real
    # homogeneity). Recompute only as a fallback for older reports.
    execution_evidence = baselines.get("execution_evidence") if isinstance(baselines, dict) else None
    distinctness_status_reported = baselines.get("baseline_distinctness_status") if isinstance(baselines, dict) else None
    if baseline_rows:
        distinctness = baselines.get("baseline_distinctness") if isinstance(baselines.get("baseline_distinctness"), dict) else check_baseline_distinctness(baseline_rows)
    else:
        distinctness = {"status": "warning", "warnings": ["baseline_report_missing_or_empty"], "duplicated_strategy_groups": []}
    distinctness_status = str(distinctness_status_reported or distinctness.get("status") or "warning")
    # Execution evidence: sufficient / insufficient / not-evaluable. An empty
    # equity curve is not strategy homogeneity, so distinctness is only trusted
    # as a real-homogeneity signal when execution evidence is sufficient.
    if not isinstance(execution_evidence, dict) and baseline_rows:
        # Legacy report without the structured field: recompute from rows so an
        # empty-equity-curve run is diagnosed as insufficient execution evidence
        # rather than being mislabeled strategy homogeneity.
        execution_evidence = classify_execution_evidence(baseline_rows, panel_empty=False)
    if isinstance(execution_evidence, dict):
        execution_status = str(execution_evidence.get("status") or "unknown")
        execution_reason = str(execution_evidence.get("reason") or "")
        distinctness_evaluable = bool(execution_evidence.get("distinctness_evaluable"))
    else:
        execution_status = "unknown"
        execution_reason = ""
        distinctness_evaluable = distinctness_status not in {"not_evaluable"}
    insufficient_execution_evidence = (
        distinctness_status == "not_evaluable"
        or (isinstance(execution_evidence, dict) and not distinctness_evaluable)
    )
    real_strategy_homogeneity = distinctness_status == "failed" and distinctness_evaluable and not insufficient_execution_evidence
    expected_count = capital.get("expected_result_count")
    result_count = capital.get("result_count")
    capital_count_ok = expected_count is not None and result_count == expected_count
    drawdown_status = capital.get("drawdown_sanity_status") or "warning"
    sharpe_status = capital.get("sharpe_sanity_status") or "warning"
    if baseline_rows:
        baseline_sanity = [validate_performance_metrics_sanity(row.get("metrics") or {}, row.get("equity_curve") or []) for row in baseline_rows]
        if any(item.get("status") == "failed" for item in baseline_sanity):
            drawdown_status = "failed"
    reports_checked = []
    provenance_missing = False
    temporary_data_pollutes_latest = False
    non_canonical_latest_reports: list[str] = []
    for name, path, payload in [
        ("strategy_viability_baselines", baseline_path, baselines),
        ("capital_lot_sensitivity", cap_path, capital),
        ("strategy_viability_report", report_path, report),
    ]:
        provenance = payload.get("report_provenance") if isinstance(payload, dict) else None
        missing = not isinstance(provenance, dict)
        provenance_missing = provenance_missing or missing
        prov = provenance if isinstance(provenance, dict) else {}
        # Latest-truthfulness: a *_latest report whose provenance says it was not
        # eligible (temporary / fixture / synthetic / smoke / unknown data root)
        # must not be trusted as canonical evidence. Only flag when the file
        # actually exists as canonical latest evidence on disk.
        data_root_kind = prov.get("data_root_kind")
        is_canonical = prov.get("is_canonical_data_root")
        latest_eligible = prov.get("latest_eligible")
        latest_blocked_reason = prov.get("latest_write_blocked_reason")
        # Legacy fallback: reports written before the truthfulness gate lack the
        # structured fields but still record args.data_root. Classify that path
        # (resolved, not string-matched) so an older temp-polluted *_latest is
        # still caught instead of being silently trusted.
        if data_root_kind is None:
            legacy_data_root = (prov.get("args") or {}).get("data_root")
            if legacy_data_root:
                classification = classify_data_root_kind(legacy_data_root)
                data_root_kind = classification["data_root_kind"]
                if is_canonical is None:
                    is_canonical = classification["is_canonical_data_root"]
        # Treat an explicit False (not merely a missing legacy field) as untrusted.
        latest_untrusted = path.exists() and (
            latest_eligible is False
            or is_canonical is False
            or (data_root_kind is not None and data_root_kind != "canonical")
        )
        if latest_untrusted:
            temporary_data_pollutes_latest = True
            non_canonical_latest_reports.append(name)
        reports_checked.append({
            "name": name,
            "path": _rel(path),
            "exists": path.exists(),
            "result_count": payload.get("result_count") if isinstance(payload, dict) else None,
            "expected_result_count": payload.get("expected_result_count") if isinstance(payload, dict) else None,
            "profile": payload.get("profile") if isinstance(payload, dict) else None,
            "pit_mode": payload.get("pit_mode") if isinstance(payload, dict) else None,
            "report_provenance_missing": missing,
            "data_root_kind": data_root_kind,
            "is_canonical_data_root": is_canonical,
            "latest_eligible": latest_eligible,
            "latest_write_blocked_reason": latest_blocked_reason,
            "latest_untrusted": latest_untrusted,
            "report_provenance": prov,
        })
    manifest = write_rewrite_manifest_outputs(build_import_rewrite_manifest())
    root_causes: list[str] = []
    fix_scope: list[str] = []
    reason_codes: list[str] = []
    audit_warnings: list[str] = []
    # (1) Insufficient execution evidence: zero trades / empty equity curves.
    #     This is NOT strategy homogeneity and must not select a best strategy;
    #     it blocks any positive strategy conclusion on its own.
    if insufficient_execution_evidence:
        reason_codes.append("insufficient_execution_evidence")
        root_causes.append("insufficient_execution_evidence_zero_trades")
        fix_scope.append("execution_feasibility_diagnosis_only")
        detail = execution_reason or "no_executable_trades"
        audit_warnings.append(f"insufficient_execution_evidence:{detail}")
    # (2) Real strategy homogeneity: only when there WAS enough execution
    #     evidence and baselines still collapsed to identical results.
    if real_strategy_homogeneity:
        reason_codes.append("real_strategy_homogeneity")
        root_causes.append("strategy_scoring_degenerated_with_execution_evidence")
        fix_scope.append("baseline_distinctness_guard")
        audit_warnings.append("real_strategy_homogeneity_detected")
    elif distinctness_status == "failed" and not insufficient_execution_evidence:
        # Distinctness failed but evaluability is unknown (legacy report) -> keep
        # the older, less specific signal rather than asserting homogeneity.
        reason_codes.append("distinctness_failed_execution_evidence_unknown")
        root_causes.append("strategy_scoring_degenerated_or_universe_too_small")
        fix_scope.append("baseline_distinctness_guard")
    # (3) Temporary / non-canonical data polluting a *_latest report.
    if temporary_data_pollutes_latest:
        reason_codes.append("temporary_data_pollutes_latest")
        root_causes.append("non_canonical_data_root_written_as_latest")
        fix_scope.append("latest_output_truthfulness_gate")
        audit_warnings.append(
            "temporary_data_pollutes_latest:" + ",".join(non_canonical_latest_reports)
        )
    if not capital_count_ok:
        reason_codes.append("capital_lot_grid_incomplete")
        root_causes.append("capital_lot_sensitivity_incomplete_grid_or_smoke_pollution")
        fix_scope.append("capital_lot_grid_integrity")
    if provenance_missing:
        reason_codes.append("latest_report_provenance_missing")
        root_causes.append("latest_report_provenance_missing")
        fix_scope.append("report_provenance_and_write_latest_hygiene")
    if manifest.get("suspected_import_rewrite_root_cause"):
        reason_codes.append("core_strategy_file_import_rewrite_present")
        root_causes.append("core_strategy_file_import_rewrite_present")
        fix_scope.append("review_core_strategy_import_rewrite_only")
    # A latest report is trustworthy for positive strategy conclusions only when
    # execution evidence is sufficient AND no temporary data polluted latest.
    latest_trustworthy_for_strategy_conclusion = (
        not insufficient_execution_evidence
        and not temporary_data_pollutes_latest
        and not provenance_missing
    )
    status = "passed"
    if root_causes:
        status = "warning"
    if (
        real_strategy_homogeneity
        or insufficient_execution_evidence
        or temporary_data_pollutes_latest
        or (distinctness_status == "failed" and not insufficient_execution_evidence)
        or not capital_count_ok
        or provenance_missing
    ):
        status = "failed"
    payload = {
        "ok": status != "failed",
        "status": status,
        "task_type": "strategy_regression_audit",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "core_strategy_files_changed_since_v3704": _changed_core_files(),
        "latest_reports_checked": reports_checked,
        "baseline_distinctness_status": distinctness.get("status"),
        "baseline_distinctness": distinctness,
        "capital_lot_result_count_status": _status_from_bool(capital_count_ok),
        "drawdown_sanity_status": drawdown_status,
        "sharpe_sanity_status": sharpe_status,
        "top_level_metrics_consistency": baselines.get("top_level_metrics_consistency") or "warning",
        "report_provenance_status": "failed" if provenance_missing else "passed",
        "execution_evidence_status": execution_status,
        "execution_evidence_reason": execution_reason,
        "insufficient_execution_evidence": insufficient_execution_evidence,
        "real_strategy_homogeneity": real_strategy_homogeneity,
        "temporary_data_pollutes_latest": temporary_data_pollutes_latest,
        "non_canonical_latest_reports": non_canonical_latest_reports,
        "latest_trustworthy_for_strategy_conclusion": latest_trustworthy_for_strategy_conclusion,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "suspected_root_causes": root_causes,
        "recommended_fix_scope": list(dict.fromkeys(fix_scope)),
        "import_rewrite_manifest": manifest,
        "warnings": list(dict.fromkeys(root_causes)),
        "output_paths": {},
    }
    return _json_safe(payload)


def render_strategy_regression_audit_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Strategy Regression Audit",
        "",
        f"- status: `{payload.get('status')}`",
        f"- baseline_distinctness_status: `{payload.get('baseline_distinctness_status')}`",
        f"- capital_lot_result_count_status: `{payload.get('capital_lot_result_count_status')}`",
        f"- drawdown_sanity_status: `{payload.get('drawdown_sanity_status')}`",
        f"- sharpe_sanity_status: `{payload.get('sharpe_sanity_status')}`",
        f"- report_provenance_status: `{payload.get('report_provenance_status')}`",
        f"- execution_evidence_status: `{payload.get('execution_evidence_status')}`",
        f"- insufficient_execution_evidence: `{payload.get('insufficient_execution_evidence')}`",
        f"- real_strategy_homogeneity: `{payload.get('real_strategy_homogeneity')}`",
        f"- temporary_data_pollutes_latest: `{payload.get('temporary_data_pollutes_latest')}`",
        f"- latest_trustworthy_for_strategy_conclusion: `{payload.get('latest_trustworthy_for_strategy_conclusion')}`",
        "",
        "## Reason Codes",
        "",
    ]
    lines.extend([f"- {item}" for item in payload.get("reason_codes") or []] or ["- none"])
    lines.extend(["", "## Suspected Root Causes", ""])
    lines.extend([f"- {item}" for item in payload.get("suspected_root_causes") or []] or ["- none"])
    lines.extend(["", "## Recommended Fix Scope", ""])
    lines.extend([f"- {item}" for item in payload.get("recommended_fix_scope") or []] or ["- none"])
    lines.extend(["", "## Latest Reports Checked", ""])
    for item in payload.get("latest_reports_checked") or []:
        lines.append(
            f"- `{item.get('name')}` path=`{item.get('path')}` "
            f"data_root_kind=`{item.get('data_root_kind')}` "
            f"latest_eligible=`{item.get('latest_eligible')}` "
            f"latest_untrusted=`{item.get('latest_untrusted')}` "
            f"provenance_missing=`{item.get('report_provenance_missing')}`"
        )
    return "\n".join(lines) + "\n"


def write_strategy_regression_audit_outputs(payload: dict[str, Any]) -> dict[str, Any]:
    RESULT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": _rel(RESULT_JSON), "markdown": _rel(REPORT_MD)}
    RESULT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    REPORT_MD.write_text(render_strategy_regression_audit_markdown(payload), encoding="utf-8")
    return payload
